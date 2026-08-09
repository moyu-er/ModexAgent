# ruff: noqa: ANN401

"""`GraphOrchestrator` — framework-level graph orchestration service.

Wires the full graph lifecycle:

    GraphSpec → GraphSpecCompiler → CompiledGraph → GraphInstance → GraphEngine

The orchestrator is the framework-level service that the bot factory
(``examples/bot_project/``) calls. It does NOT know about specific node
types; callers inject a node registry and a state-class mapping. This is the
second consumer of
``GraphSpecCompiler`` after the compiler's own tests — a real seam).

Provides:

- ``create_instance(spec_id)`` — load spec, compile, create instance (PENDING),
  register in ``_active_instances``, return ``graph_instance_id``. Returns
  immediately without executing — the caller runs the graph separately via
  ``run_instance``.
- ``run_instance(graph_instance_id)`` — execute a created instance. Sets
  status to RUNNING, runs the engine, emits ``GraphOutput`` in finally,
  and removes terminal instances from ``_active_instances``.
- ``create_and_run(spec_id)`` — convenience: ``create_instance`` +
  ``run_instance``. Retained for backward compatibility with tests.
- ``get_state(graph_instance_id)`` — delegate to ``coordinator.get_graph_state``
  for active instances; for inactive (evicted) instances, load metadata
  from ``instance_store`` and query via a temporary coordinator.
- ``pause`` / ``stop`` / ``resume`` / ``deliver_to_node`` — external control,
  all delegated to ``GraphControlService`` via ``ControlCommand`` (rule 15:
  single control path — REST / CLI / orchestrator all converge on the same
  ``GraphControlService.handle`` path).
- ``recover_crashed`` — fault recovery, delegated to ``GraphRecoveryService``.

``GraphRecoveryService`` calls ``_run_existing_instance`` directly
(rule 15: converge — single recovery path, no adapter indirection).
The orchestrator IS the engine factory; recovery delegates to the
internal 7-step re-registration path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

from modex_agent.control.graph_control import (
    GraphControlService,
    LiveGraphEngineController,
)
from modex_agent.control.graph_recovery import GraphRecoveryService
from modex_agent.control.types import ControlCommand, ControlCommandType, ControlScope
from modex_graph import (
    CoordinatorFactory,
    GraphContext,
    GraphDrained,
    GraphEngine,
    GraphIORecord,
    GraphIORecordStore,
    GraphInstance,
    GraphInstanceStatus,
    GraphInstanceStore,
    GraphInterrupt,
    GraphMetadata,
    GraphOutput,
    GraphOutputAdapter,
    GraphOutputKind,
    GraphPayload,
    GraphPersistenceCoordinator,
    GraphRuntime,
    GraphSpec,
    GraphSpecCompiler,
    GraphSpecStore,
    GraphState,
    GraphStateSnapshot,
    NodeRegistry,
    NullCoordinatorFactory,
    NullGraphIORecordStore,
    default_id_generator,
)
from modex_graph.persistence._time import now_ms

logger = logging.getLogger(__name__)

# ControlScope.session_id placeholder for orchestrator-issued commands.
# The graph control path (GraphControlService._pause / _stop / _resume /
# _deliver) uses only scope.graph_instance_id; session_id is required by the
# ControlScope dataclass but unused for graph-scoped commands.
_ORCHESTRATOR_SESSION_ID = "_orchestrator"
_NULL_COORDINATOR_FACTORY = NullCoordinatorFactory()


class GraphOrchestrator:
    """Framework-level graph orchestration service.

    Wires ``GraphSpec`` → ``CompiledGraph`` → ``GraphInstance`` →
    ``GraphEngine`` execution. Provides external control via
    ``GraphControlService``. Provides recovery via ``GraphRecoveryService``.

    The orchestrator does not know specific node or state implementations;
    callers inject the node registry and state-class mapping.

    Registry:

    - ``_active_instances: dict[int, GraphInstance]`` — tracks live
      ``GraphInstance`` objects keyed by ``graph_instance_id``. The
      coordinator lifecycle is bound to the ``GraphInstance`` lifecycle,
      not to the ``run_instance`` call stack.
    - ``create_instance`` creates the coordinator, registers nodes,
      constructs the ``GraphInstance`` (with ``compiled`` + ``user_input``),
      and registers it. Status is ``PENDING``.
    - ``run_instance`` sets status to ``RUNNING``, executes, and in
      ``finally`` emits ``GraphOutput`` + removes terminal instances
      (COMPLETED/CRASHED/STOPPED removed, PAUSED retained for resume).
    - ``unregister_instance`` closes the coordinator's resources
      (SQLite connections) and removes the instance from the registry.
    - ``GraphControlService._deliver`` converges on
      ``coordinator.route_deliver`` via ``_lookup_coordinator`` —
      no shared ``deliver_store``.

    Lifecycle management (``run_instance``):

    - Normal completion → ``COMPLETED`` (terminal, evicted).
    - ``GraphInterrupt`` (HITL suspend) → ``PAUSED``, re-raise. The
      ``GraphInstance`` stays in the registry (coordinator alive for resume).
    - ``GraphDrained`` (external pause/stop) → expected exit; status was
      already written by ``GraphControlService``. PAUSED retained, STOPPED
      evicted.
    - Any other exception → ``CRASHED`` (terminal, evicted), re-raise.
    - Always unregister the engine controller (cleanup) + emit output.
    """

    def __init__(
        self,
        *,
        node_registry: NodeRegistry,
        state_classes: Mapping[str, type[GraphState]],
        spec_store: GraphSpecStore,
        instance_store: GraphInstanceStore,
        coordinator_factory: CoordinatorFactory = _NULL_COORDINATOR_FACTORY,
        output_adapter: GraphOutputAdapter | None = None,
        io_store: GraphIORecordStore = NullGraphIORecordStore(),
    ) -> None:
        """Initialize the orchestrator with the required registries + stores.

        Args:
            node_registry: pre-populated by the bot factory with
                ``AgentNodeFactory``, ``FunctionNodeFactory``, etc.
            state_classes: registry names mapped to concrete graph state classes.
            spec_store: persistence for ``GraphSpec`` records.
            instance_store: persistence for ``GraphInstance`` records.
            coordinator_factory: creates the ``GraphPersistenceCoordinator``
                for each graph instance. The factory receives this
                orchestrator's ``instance_store`` and assembles the remaining
                stores internally. Defaults to ``NullCoordinatorFactory``
                (no-op persistence); business layers substitute a
                SQLite-backed factory for crash recovery.
            output_adapter: optional adapter notified when execution completes
                or crashes.
            io_store: persistence for ``GraphIORecord`` (input/output payloads
                per graph instance). A record is saved on ``create_instance``
                with the ``user_input``; its ``output`` is updated when
                ``run_instance`` completes. Defaults to
                ``NullGraphIORecordStore`` (no-op); business layers substitute
                an ``InMemoryGraphIORecordStore`` or
                ``SqliteGraphIORecordStore`` for I/O tracking.
        """
        self._node_registry = node_registry
        self._state_classes = state_classes
        self._spec_store = spec_store
        self._instance_store = instance_store
        self._coordinator_factory = coordinator_factory
        self._output_adapter = output_adapter
        self._io_store = io_store
        self._compiler = GraphSpecCompiler(node_registry, state_classes)
        self._runtime = GraphRuntime()
        self._active_instances: dict[int, GraphInstance] = {}
        self._run_tasks: set[asyncio.Task[None]] = set()
        self._running_gids: set[int] = set()

        # Wire recovery + control (rule 15: single control + recovery path).
        # GraphRecoveryService calls _run_existing_instance directly — no
        # adapter indirection. The same coordinator factory is shared by
        # both create_instance and recovery so that store-assembly strategy
        # stays consistent across both paths.
        self._recovery_service = GraphRecoveryService(
            instance_store,
            self,
            coordinator_factory=coordinator_factory,
        )
        # GraphControlService._deliver converges on
        # coordinator.route_deliver via the registry lookup — no shared
        # deliver_store.
        self._control_service = GraphControlService(
            instance_store,
            self._recovery_service,
            coordinator_lookup=self._lookup_coordinator,
            finalize_instance=self._finalize_instance,
        )

    async def create_instance(
        self,
        spec_id: int,
        *,
        parent_instance_id: int | None = None,
        user_input: GraphPayload | None = None,
    ) -> int:
        """Load spec → compile → create ``GraphInstance`` (PENDING) → register.

        Returns the ``graph_instance_id`` immediately without executing.
        The caller runs the graph separately via ``run_instance``.

        Status is set to ``PENDING``. The ``compiled`` graph and
        ``user_input`` are stored on the ``GraphInstance`` so they survive
        the create→run gap. The ``node_id_map`` is populated from
        ``compiled.nodes`` and persisted in ``GraphMetadata`` (F7).

        Raises:
            ValueError: if the spec is not found in ``spec_store``.
            TopologyError: if the spec's topology is invalid.
            KeyError: if a ``NodeSpec.node_type`` is not registered.
        """
        spec = self._load_spec(spec_id)
        compiled = self._compiler.compile(spec)
        graph_instance_id = default_id_generator().generate()
        node_id_map = {name: node.node_id for name, node in compiled.nodes.items()}
        metadata = GraphMetadata(
            graph_instance_id=graph_instance_id,
            spec_id=spec_id,
            parent_instance_id=parent_instance_id,
            parent_node=None,
            status=GraphInstanceStatus.PENDING,
            node_id_map=node_id_map,
        )
        self._instance_store.save(metadata)
        self._io_store.save(
            GraphIORecord(
                record_id=default_id_generator().generate(),
                graph_instance_id=graph_instance_id,
                spec_id=spec_id,
                user_input=user_input,
                output=None,
                created_at=now_ms(),
            )
        )
        coordinator = self._coordinator_factory.create(graph_instance_id, self._instance_store)
        self._attach_output_adapter(coordinator)
        for node in compiled.nodes.values():
            coordinator.register_node(node.node_id)
        instance = GraphInstance(
            metadata,
            coordinator,
            compiled=compiled,
            user_input=user_input,
        )
        self._active_instances[graph_instance_id] = instance
        return graph_instance_id

    async def run_instance(self, graph_instance_id: int) -> None:
        """Execute a created ``GraphInstance``.

        Sets status to ``RUNNING``, creates state + ``GraphContext``
        (with ``user_input`` from the instance), runs the engine, and
        in ``finally`` emits ``GraphOutput`` + removes terminal
        instances from ``_active_instances`` (COMPLETED/CRASHED/STOPPED
        removed, PAUSED retained for resume).

        Raises:
            ValueError: if the instance is not in ``_active_instances``.
            GraphInterrupt: if a node suspends (HITL). Instance status is
                set to ``PAUSED`` before re-raising.
            Exception: any engine exception. Instance status is set to
                ``CRASHED`` before re-raising.
        """
        instance = self._active_instances.get(graph_instance_id)
        if instance is None:
            raise ValueError(
                f"Graph instance {graph_instance_id} not in _active_instances; "
                "call create_instance first."
            )
        if instance.compiled is None:
            raise ValueError(
                f"Graph instance {graph_instance_id} has no compiled graph; "
                "this should not happen after create_instance."
            )
        gid = graph_instance_id
        if gid in self._running_gids:
            raise ValueError(
                f"Graph instance {gid} is already running; "
                "wait for it to complete or pause before re-running."
            )
        self._running_gids.add(gid)
        self._instance_store.update_status(gid, GraphInstanceStatus.RUNNING)
        output: GraphOutput | None = None
        status = GraphInstanceStatus.RUNNING
        ctx: GraphContext[Any] | None = None
        try:
            spec = self._load_spec(instance.spec_id)
            state = (
                instance.initial_state
                if instance.initial_state is not None
                else self._create_state(spec)
            )
            compiled = instance.compiled
            engine: GraphEngine[Any] = GraphEngine(compiled)
            ctx = GraphContext(
                state=state,
                runtime=self._runtime,
                coordinator=instance.coordinator,
                user_input=instance.user_input,
                graph_instance_id=gid,
            )
            controller = LiveGraphEngineController(gid, ctx.control)
            self._control_service.register_engine(controller)

            final_state = await engine.run_async(ctx)
            status = GraphInstanceStatus.COMPLETED
            self._instance_store.update_status(gid, status)
            result = dict(final_state).get("result")
            io_record = self._io_store.get_by_instance(gid)
            if io_record is not None:
                # result is Any (dict extraction); narrow at the state boundary.
                self._io_store.update_output(
                    io_record.record_id, result if isinstance(result, list) else None,
                )
            output = GraphOutput(
                kind=GraphOutputKind.COMPLETED,
                graph_instance_id=gid,
                result=result,
                timestamp=time.time_ns() // 1_000_000,
            )
        except GraphInterrupt:
            status = GraphInstanceStatus.PAUSED
            self._instance_store.update_status(gid, status)
            raise
        except GraphDrained:
            status = (
                GraphInstanceStatus.STOPPED
                if ctx is not None and ctx.control.stop_requested
                else GraphInstanceStatus.PAUSED
            )
            self._instance_store.update_status(gid, status)
        except Exception as exc:
            status = GraphInstanceStatus.CRASHED
            self._instance_store.update_status(gid, status)
            output = GraphOutput(
                kind=GraphOutputKind.CRASHED,
                graph_instance_id=gid,
                error=str(exc),
                timestamp=time.time_ns() // 1_000_000,
            )
            raise
        finally:
            await self._finalize_instance(gid, status, output=output)
            self._running_gids.discard(gid)

    async def create_and_run(
        self,
        spec_id: int,
        *,
        initial_state: GraphState | None = None,
        parent_instance_id: int | None = None,
        user_input: GraphPayload | None = None,
    ) -> int:
        """Convenience: ``create_instance`` + ``run_instance`` (for tests).

        Returns the ``graph_instance_id`` (for external control via
        ``pause`` / ``stop`` / ``resume`` / ``deliver_to_node``).
        """
        gid = await self.create_instance(
            spec_id,
            parent_instance_id=parent_instance_id,
            user_input=user_input,
        )
        if initial_state is not None:
            self._active_instances[gid].initial_state = initial_state
        task = self.start_run(gid)
        await task  # sync wait for tests; task is tracked in _run_tasks
        return gid

    def start_run(self, graph_instance_id: int) -> asyncio.Task[None]:
        """Launch ``run_instance`` as a tracked background task (non-blocking).

        The task is stored in ``_run_tasks`` so ``cleanup`` can cancel and
        await it before closing connections. Used by REST handlers (run,
        resume) so the HTTP request returns immediately while the graph
        executes in the background — same path as normal scheduling.

        Returns the created task so callers (e.g. ``create_and_run``) can
        await it for synchronous completion while still tracking it in
        ``_run_tasks``.
        """
        task = asyncio.create_task(self.run_instance(graph_instance_id))
        self._run_tasks.add(task)
        task.add_done_callback(self._run_tasks.discard)
        return task

    def start_resume(self, graph_instance_id: int) -> asyncio.Task[None]:
        """Launch ``resume`` as a tracked background task (non-blocking).

        Delegates to ``resume`` which routes through the recovery path
        (``GraphControlService`` → ``GraphRecoveryService`` →
        ``_run_existing_instance`` → ``run_instance`` → ``bootstrap``),
        rebuilding the instance if it was evicted from
        ``_active_instances`` (e.g. after a bot restart). Used by REST
        handlers so the HTTP request returns immediately while the graph
        resumes in the background — same task-tracking path as
        ``start_run``.

        Returns the created task so callers can await it for synchronous
        completion while still tracking it in ``_run_tasks``.
        """
        task = asyncio.create_task(self.resume(graph_instance_id))
        self._run_tasks.add(task)
        task.add_done_callback(self._run_tasks.discard)
        return task

    def get_state(self, graph_instance_id: int) -> GraphStateSnapshot:
        """Collect graph metadata + per-node version histories.

        Delegates to ``coordinator.get_graph_state`` for active instances.
        For inactive (evicted) instances, loads metadata from
        ``instance_store`` and queries via a temporary coordinator
        constructed from ``coordinator_factory`` + ``node_id_map``.
        """
        instance = self._active_instances.get(graph_instance_id)
        if instance is not None:
            return instance.get_state()
        metadata = self._instance_store.load(graph_instance_id)
        if metadata is None:
            raise ValueError(f"Graph instance {graph_instance_id} not found in instance_store.")
        coordinator = self._coordinator_factory.create(graph_instance_id, self._instance_store)
        for node_id in metadata.node_id_map.values():
            coordinator.register_node(node_id)
        return coordinator.get_graph_state()

    async def pause(self, graph_instance_id: int) -> None:
        """Pause the graph instance (delegates to ``GraphControlService``)."""
        await self._control_service.handle(
            self._make_command(ControlCommandType.PAUSE_GRAPH, graph_instance_id)
        )

    async def stop(self, graph_instance_id: int) -> None:
        """Stop the graph instance (delegates to ``GraphControlService``)."""
        await self._control_service.handle(
            self._make_command(ControlCommandType.STOP_GRAPH, graph_instance_id)
        )

    async def resume(self, graph_instance_id: int) -> None:
        """Resume a paused graph instance.

        Delegates to ``GraphControlService`` → ``GraphRecoveryService.resume``.
        The recovery service validates the status (PAUSED only — STOPPED
        is terminal and cannot be resumed), sets it to RUNNING, and calls
        ``_run_existing_instance`` to re-run the graph (state is restored
        via ``bootstrap`` inside ``run_async``).
        """
        await self._control_service.handle(
            self._make_command(ControlCommandType.RESUME_GRAPH, graph_instance_id)
        )

    async def deliver_to_node(
        self,
        graph_instance_id: int,
        node_name: str,
        content: Any,
    ) -> None:
        """Deliver content to a node in the graph instance.

        Delegates to ``GraphControlService`` which persists the deliver in
        ``DeliverStore`` and notifies the engine controller. The node picks
        up the deliver on its next ``_collect_delivers`` call.
        """
        await self._control_service.handle(
            self._make_command(
                ControlCommandType.DELIVER_TO_NODE,
                graph_instance_id,
                payload={"node_name": node_name, "content": content},
            )
        )

    async def recover_crashed(self) -> list[int]:
        """Fault recovery: auto-recover all CRASHED instances.

        Delegates to ``GraphRecoveryService.recover_crashed``. Called on
        startup (or on-demand by an operator) to auto-recover instances
        that crashed mid-execution.

        Returns:
            The list of recovered ``graph_instance_id``s.
        """
        return await self._recovery_service.recover_crashed()

    async def pause_all_active(self) -> None:
        """Pause every active graph whose persisted status is RUNNING."""
        for graph_instance_id, _instance in tuple(self._active_instances.items()):
            metadata = self._instance_store.load(graph_instance_id)
            if metadata is not None and metadata.status is GraphInstanceStatus.RUNNING:
                await self.pause(graph_instance_id)

    async def cleanup(self) -> None:
        """Cancel tracked run tasks, then close coordinators and clear registries."""
        for task in list(self._run_tasks):
            task.cancel()
        if self._run_tasks:
            await asyncio.gather(*self._run_tasks, return_exceptions=True)
        self._run_tasks.clear()
        for instance in self._active_instances.values():
            instance.coordinator.close()
        self._active_instances.clear()

    def unregister_instance(self, graph_instance_id: int) -> None:
        """Evict a GraphInstance from the registry.

        Calls ``coordinator.close()`` (a no-op — the coordinator owns no
        connections; the caller manages the ``sqlite3.Connection`` lifetime)
        and removes the instance from ``_active_instances``. Safe to call
        on an unregistered ID (no-op).

        Triggered by:
        - Crash recovery: old instance evicted before registering the new
          one (``_run_existing_instance``).
        - Explicit application cleanup (e.g. shutdown hooks).
        """
        instance = self._active_instances.pop(graph_instance_id, None)
        if instance is not None:
            instance.coordinator.close()

    def _evict_if_terminal(
        self,
        graph_instance_id: int,
        status: GraphInstanceStatus,
    ) -> None:
        """Remove terminal instances from ``_active_instances``.

        After ``run_instance`` completes (or crashes), if the run outcome is
        COMPLETED, CRASHED, or STOPPED, the instance is evicted
        (coordinator closed + removed from registry). PAUSED instances are
        retained for resume.
        """
        if status in {
            GraphInstanceStatus.COMPLETED,
            GraphInstanceStatus.CRASHED,
            GraphInstanceStatus.STOPPED,
            GraphInstanceStatus.FAILED,
        }:
            self.unregister_instance(graph_instance_id)

    async def _finalize_instance(
        self,
        graph_instance_id: int,
        status: GraphInstanceStatus,
        *,
        output: GraphOutput | None = None,
    ) -> None:
        """Unified finalization for all terminal and non-terminal transitions.

        Called from ``run_instance`` finally (engine path) and
        ``GraphControlService._stop`` (no-engine path for paused→stop).
        Converges unregister_engine + emit + evict into one method so
        every status transition goes through the same cleanup.

        emit failures are isolated (logged, not re-raised) so they
        never prevent eviction.
        """
        self._control_service.unregister_engine(graph_instance_id)
        if output is not None and self._output_adapter is not None:
            instance = self._active_instances.get(graph_instance_id)
            if instance is not None:
                # Flush pending fire-and-forget node-level events so the
                # terminal event is emitted last (causal ordering).
                await instance.coordinator.drain_output_events()
            try:
                await self._output_adapter.emit(output)
            except Exception:
                logger.warning(
                    "output adapter emit failed for instance %s",
                    graph_instance_id,
                    exc_info=True,
                )
        self._evict_if_terminal(graph_instance_id, status)

    # ── Internal: recovery path (called by GraphRecoveryService) ──────

    async def _run_existing_instance(self, instance: GraphInstance) -> None:
        """Recovery path: load spec, compile, node_id recovery, run.

        Called by ``GraphRecoveryService._recover_instances`` when
        recovering a crashed/paused/stopped instance. The recovery service
        creates the ``GraphInstance`` with a fresh coordinator (via
        ``coordinator_factory.create``); this method handles:

        - If an old ``GraphInstance`` with the same gid is still in the
          registry, ``unregister_instance`` it (closes the old coordinator)
          before registering the new one.
        - After compiling the spec, restore ``node_id`` for each
          ``compiled.nodes`` entry from ``metadata.node_id_map`` so persisted
          node_states/deliver_states match after recovery.
        - Register nodes on ``instance.coordinator`` by restored node_id.
        - Store ``compiled`` on the instance for ``run_instance``.
        - Register in ``_active_instances`` and delegate to ``run_instance``.

        The scheduler restores state via ``bootstrap(ctx, graph)``
        inside ``run_async``.
        """
        gid = instance.graph_instance_id
        if gid in self._active_instances:
            self.unregister_instance(gid)
        spec = self._load_spec(instance.spec_id)
        compiled = self._compiler.compile(spec)
        self._attach_output_adapter(instance.coordinator)
        for node in compiled.nodes.values():
            node.node_id = instance.metadata.node_id_map[node.name]
        for node in compiled.nodes.values():
            instance.coordinator.register_node(node.node_id)
        instance.compiled = compiled
        self._active_instances[gid] = instance
        await self.run_instance(gid)

    # ── Internal: coordinator lookup ────────────────────────────

    def _attach_output_adapter(self, coordinator: GraphPersistenceCoordinator) -> None:
        """Wire this orchestrator's output adapter into a coordinator.

        Single post-assembly wiring point for node-level ``GraphOutput``
        events — called by both ``create_instance`` and the recovery path
        (``_run_existing_instance``) so every runnable instance emits
        through the same seam (rule 15).
        """
        coordinator.set_output_adapter(self._output_adapter)

    def _lookup_coordinator(self, graph_instance_id: int) -> GraphPersistenceCoordinator | None:
        """Look up the coordinator for an active graph instance.

        Used by ``GraphControlService._deliver`` to route external delivers
        through ``coordinator.route_deliver`` (no shared deliver_store).
        Returns ``None`` if the instance is not in the registry.
        """
        instance = self._active_instances.get(graph_instance_id)
        return instance.coordinator if instance is not None else None

    # ── Internal: spec + state helpers ─────────────────────────────────

    def _load_spec(self, spec_id: int) -> GraphSpec:
        """Load a ``GraphSpec`` by ID, raising if not found."""
        spec = self._spec_store.load_by_id(spec_id)
        if spec is None:
            raise ValueError(
                f"GraphSpec {spec_id} not found in spec_store. "
                f"Cannot create or recover graph instance."
            )
        return spec

    def _create_state(self, spec: GraphSpec) -> GraphState:
        """Create fresh state from the class selected by the serialized spec."""
        return self._state_classes[spec.state_class]()

    # ── Internal: control command construction ─────────────────────────

    def _make_command(
        self,
        cmd_type: ControlCommandType,
        graph_instance_id: int,
        *,
        payload: dict[str, object] | None = None,
    ) -> ControlCommand:
        """Build a ``ControlCommand`` for the given graph instance.

        All orchestrator-issued control commands converge on the same
        ``GraphControlService.handle`` path (rule 15). The ``session_id``
        placeholder satisfies the ``ControlScope`` dataclass requirement;
        the graph control path uses only ``graph_instance_id``.
        """
        return ControlCommand(
            command_id=f"orchestrator-{graph_instance_id}-{cmd_type.value}",
            type=cmd_type,
            scope=ControlScope(
                session_id=_ORCHESTRATOR_SESSION_ID,
                graph_instance_id=graph_instance_id,
            ),
            payload=payload if payload is not None else {},
        )


__all__ = ["GraphOrchestrator"]
