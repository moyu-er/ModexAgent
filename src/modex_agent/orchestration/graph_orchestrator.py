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

- ``create_and_run(spec_id)`` — load spec, compile, create instance, run.
  Returns the ``graph_instance_id`` for external control.
- ``pause`` / ``stop`` / ``resume`` / ``deliver_to_node`` — external control,
  all delegated to ``GraphControlService`` via ``ControlCommand`` (rule 15:
  single control path — REST / CLI / orchestrator all converge on the same
  ``GraphControlService.handle`` path).
- ``recover_crashed`` — fault recovery, delegated to ``GraphRecoveryService``.

The orchestrator provides a ``GraphEngineFactory`` implementation (via an
internal ``_EngineFactoryAdapter``) so ``GraphRecoveryService`` can create
engines for recovered instances. The adapter is needed because the
orchestrator's public ``create_and_run`` takes ``spec_id`` (creating a new
instance), while the recovery flow passes an existing ``GraphInstance``.
Composition over inheritance resolves the signature conflict without
type-branching (rule 15: converge).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from modex_agent.control.graph_control import (
    GraphControlService,
    LiveGraphEngineController,
)
from modex_agent.control.graph_recovery import GraphEngineFactory, GraphRecoveryService
from modex_agent.control.types import ControlCommand, ControlCommandType, ControlScope
from modex_graph import (
    CompiledGraph,
    CoordinatorFactory,
    GraphContext,
    GraphDrained,
    GraphEngine,
    GraphInstance,
    GraphInstanceStatus,
    GraphInstanceStore,
    GraphInterrupt,
    GraphMetadata,
    GraphPersistenceCoordinator,
    GraphRuntime,
    GraphSpec,
    GraphSpecCompiler,
    GraphSpecStore,
    GraphState,
    NodeRegistry,
    NullCoordinatorFactory,
    default_id_generator,
)

logger = logging.getLogger(__name__)

# ControlScope.session_id placeholder for orchestrator-issued commands.
# The graph control path (GraphControlService._pause / _stop / _resume /
# _deliver) uses only scope.graph_instance_id; session_id is required by the
# ControlScope dataclass but unused for graph-scoped commands.
_ORCHESTRATOR_SESSION_ID = "_orchestrator"
_NULL_COORDINATOR_FACTORY = NullCoordinatorFactory()


class _EngineFactoryAdapter(GraphEngineFactory):
    """Adapts ``GraphOrchestrator`` to the ``GraphEngineFactory`` interface (rule 7).

    The orchestrator's public ``create_and_run`` takes ``spec_id`` (creating a
    new instance) and returns the ``graph_instance_id``. The recovery flow
    (``GraphRecoveryService._recover_instances``) calls
    ``engine_factory.create_and_run(instance)`` — passing an existing
    ``GraphInstance`` and expecting ``None``.

    The signature conflict (``int`` vs ``GraphInstance``, ``int`` vs ``None``
    return) makes direct inheritance impossible without type-branching.
    This adapter bridges the two: ``GraphRecoveryService`` calls the adapter,
    which delegates to the orchestrator's internal ``_run_existing_instance``.

    This is composition over inheritance — the adapter IS a
    ``GraphEngineFactory`` subclass (rule 7), and the orchestrator provides
    the implementation logic. The adapter is the single seam (rule 6).
    """

    def __init__(self, orchestrator: GraphOrchestrator) -> None:
        self._orchestrator = orchestrator

    async def create_and_run(self, instance: GraphInstance) -> None:
        await self._orchestrator._run_existing_instance(instance)


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
      not to the ``_execute`` call stack.
    - ``create_and_run`` creates the coordinator, registers nodes,
      constructs the ``GraphInstance``, and registers it before ``_execute``.
    - ``unregister_instance`` closes the coordinator's resources
      (SQLite connections) and removes the instance from the registry.
    - ``GraphControlService._deliver`` converges on
      ``coordinator.route_deliver`` via ``_lookup_coordinator`` —
      no shared ``deliver_store``.

    Lifecycle management (``_execute``):

    - Normal completion → ``instance.status = COMPLETED``.
    - ``GraphInterrupt`` (HITL suspend) → ``instance.status = PAUSED``,
      re-raise so the caller knows the graph suspended. The
      ``GraphInstance`` stays in the registry (coordinator alive for resume).
    - Any other exception → ``instance.status = CRASHED``, re-raise.
    - Always unregister the engine controller (cleanup). The
      ``GraphInstance`` stays in the registry until explicitly evicted
      via ``unregister_instance``.
    """

    def __init__(
        self,
        *,
        node_registry: NodeRegistry,
        state_classes: Mapping[str, type[GraphState]],
        spec_store: GraphSpecStore,
        instance_store: GraphInstanceStore,
        coordinator_factory: CoordinatorFactory = _NULL_COORDINATOR_FACTORY,
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
        """
        self._node_registry = node_registry
        self._state_classes = state_classes
        self._spec_store = spec_store
        self._instance_store = instance_store
        self._coordinator_factory = coordinator_factory
        self._compiler = GraphSpecCompiler(node_registry, state_classes)
        self._runtime = GraphRuntime()
        self._active_instances: dict[int, GraphInstance] = {}

        # Wire recovery + control (rule 15: single control + recovery path).
        # The adapter lets GraphRecoveryService call back into the orchestrator
        # to create engines for recovered instances. The same coordinator
        # factory is shared by both create_and_run and recovery so that
        # store-assembly strategy stays consistent across both paths.
        self._engine_factory = _EngineFactoryAdapter(self)
        self._recovery_service = GraphRecoveryService(
            instance_store,
            self._engine_factory,
            coordinator_factory=coordinator_factory,
        )
        # GraphControlService._deliver converges on
        # coordinator.route_deliver via the registry lookup — no shared
        # deliver_store.
        self._control_service = GraphControlService(
            instance_store,
            self._recovery_service,
            coordinator_lookup=self._lookup_coordinator,
        )

    async def create_and_run(
        self,
        spec_id: int,
        *,
        initial_state: GraphState | None = None,
        parent_instance_id: int | None = None,
    ) -> int:
        """Load ``GraphSpec`` → compile → create ``GraphInstance`` → run.

        Returns the ``graph_instance_id`` (for external control via
        ``pause`` / ``stop`` / ``resume`` / ``deliver_to_node``).

        Raises:
            ValueError: if the spec is not found in ``spec_store``.
            TopologyError: if the spec's topology is invalid.
            KeyError: if a ``NodeSpec.node_type`` is not registered.
            GraphInterrupt: if a node suspends (HITL). Instance status is
                set to ``PAUSED`` before re-raising.
            Exception: any engine exception. Instance status is set to
                ``CRASHED`` before re-raising.
        """
        spec = self._load_spec(spec_id)
        compiled = self._compiler.compile(spec)
        graph_instance_id = default_id_generator().generate()
        metadata = GraphMetadata(
            graph_instance_id=graph_instance_id,
            spec_id=spec_id,
            parent_instance_id=parent_instance_id,
            parent_node=None,
            status=GraphInstanceStatus.RUNNING,
        )
        self._instance_store.save(metadata)
        coordinator = self._coordinator_factory.create(graph_instance_id, self._instance_store)
        for node_name in compiled.nodes:
            coordinator.register_node(node_name)
        instance = GraphInstance(metadata, coordinator)
        self._active_instances[graph_instance_id] = instance
        state = initial_state if initial_state is not None else self._create_state(spec)
        await self._execute(instance, compiled, state)
        return graph_instance_id

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
        ``_run_existing_instance`` to re-run the graph via
        ``coordinator.load_for_recovery``.
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

    # ── Internal: recovery path (called by _EngineFactoryAdapter) ───────

    async def _run_existing_instance(self, instance: GraphInstance) -> None:
        """Recovery path: load spec, compile, register nodes, run.

        Called by ``_EngineFactoryAdapter.create_and_run`` when
        ``GraphRecoveryService`` recovers a crashed/paused/stopped instance.
        The recovery service creates the ``GraphInstance`` with a fresh
        coordinator (via ``coordinator_factory.create``); this method handles:

        - If an old ``GraphInstance`` with the same gid is still in the
          registry, ``unregister_instance`` it (closes the old coordinator)
          before registering the new one.
        - Register nodes on ``instance.coordinator`` from
          ``compiled.nodes`` (before ``_execute``).
        - Register the instance in ``_active_instances``.
        - Create state + ``_execute``.

        The scheduler restores state via ``coordinator.load_for_recovery()``
        inside ``run_async``.
        """
        gid = instance.graph_instance_id
        if gid in self._active_instances:
            self.unregister_instance(gid)
        spec = self._load_spec(instance.spec_id)
        compiled = self._compiler.compile(spec)
        for node_name in compiled.nodes:
            instance.coordinator.register_node(node_name)
        self._active_instances[gid] = instance
        state = self._create_state(spec)
        await self._execute(instance, compiled, state)

    # ── Internal: shared execution ─────────────────────────────────────

    async def _execute(
        self,
        instance: GraphInstance,
        compiled: CompiledGraph[Any],
        state: GraphState,
    ) -> None:
        """Create engine + context, register controller, run, manage lifecycle.

        Lifecycle transitions:

        - Normal completion → ``COMPLETED``.
        - ``GraphInterrupt`` (HITL suspend) → ``PAUSED``, re-raise. The
          ``GraphInstance`` stays in ``_active_instances`` (coordinator
          alive for resume).
        - ``GraphDrained`` (external pause/stop) → expected exit; status was
          already written by ``GraphControlService``.
        - Any other exception → ``CRASHED``, re-raise.
        - Always unregister the engine controller (cleanup). The
          ``GraphInstance`` stays in the registry until explicitly evicted.
        """
        gid = instance.graph_instance_id
        engine: GraphEngine[Any] = GraphEngine(compiled)
        # Use the GraphInstance's coordinator (not a per-run new one).
        # Node registration was done in create_and_run / _run_existing_instance
        # (at GraphInstance construction time, before _execute).
        ctx: GraphContext[Any] = GraphContext(
            state=state,
            runtime=self._runtime,
            coordinator=instance.coordinator,
            graph_instance_id=gid,
        )
        controller = LiveGraphEngineController(gid, ctx.control)
        self._control_service.register_engine(controller)
        try:
            await engine.run_async(ctx)
            self._instance_store.update_status(gid, GraphInstanceStatus.COMPLETED)
        except GraphInterrupt:
            self._instance_store.update_status(gid, GraphInstanceStatus.PAUSED)
            raise
        except GraphDrained:
            pass
        except Exception:
            self._instance_store.update_status(gid, GraphInstanceStatus.CRASHED)
            raise
        finally:
            self._control_service.unregister_engine(gid)

    # ── Internal: coordinator lookup ────────────────────────────

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
