# ruff: noqa: ANN401

"""`GraphOrchestrator` — framework-level graph orchestration service (ticket 10 §3.6).

Wires the full graph lifecycle (ticket 10 §3.6):

    GraphSpec → GraphSpecCompiler → CompiledGraph → GraphInstance → GraphEngine

The orchestrator is the framework-level service that the bot factory
(``examples/bot_project/``) calls. It does NOT know about specific node
types — it uses the injected ``NodeRegistry`` and ``StateRegistry``, which
the bot factory pre-populates with ``AgentNodeFactory``, ``ReactStateFactory``,
etc. (rule 5: the interface is the test surface; rule 6: second consumer of
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
from typing import Any

from modex_agent.control.graph_control import (
    GraphControlService,
    GraphEngineController,
    InMemoryGraphEngineController,
)
from modex_agent.control.graph_recovery import GraphEngineFactory, GraphRecoveryService
from modex_agent.control.types import ControlCommand, ControlCommandType, ControlScope
from modex_graph import (
    CheckpointStore,
    CompiledGraph,
    DeliverStore,
    DynamicStateFactory,
    GraphContext,
    GraphEngine,
    GraphInstance,
    GraphInstanceStatus,
    GraphInstanceStore,
    GraphInterrupt,
    GraphRuntime,
    GraphSpec,
    GraphSpecCompiler,
    GraphSpecStore,
    GraphState,
    InMemoryDeliverStore,
    NodeRegistry,
    StateRegistry,
    StateSchema,
    default_id_generator,
)

logger = logging.getLogger(__name__)

# ControlScope.session_id placeholder for orchestrator-issued commands.
# The graph control path (GraphControlService._pause / _stop / _resume /
# _deliver) uses only scope.graph_instance_id; session_id is required by the
# ControlScope dataclass but unused for graph-scoped commands.
_ORCHESTRATOR_SESSION_ID = "_orchestrator"


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
    """Framework-level graph orchestration service (ticket 10 §3.6).

    Wires ``GraphSpec`` → ``CompiledGraph`` → ``GraphInstance`` →
    ``GraphEngine`` execution. Provides external control via
    ``GraphControlService``. Provides recovery via ``GraphRecoveryService``.

    The orchestrator does NOT know about specific node types — it uses the
    injected ``NodeRegistry`` and ``StateRegistry``, which the bot factory
    pre-populates with ``AgentNodeFactory``, ``ReactStateFactory``, etc.

    Lifecycle management (``_execute``):

    - Normal completion → ``instance.status = COMPLETED``.
    - ``GraphInterrupt`` (HITL suspend) → ``instance.status = PAUSED``,
      re-raise so the caller knows the graph suspended.
    - Any other exception → ``instance.status = CRASHED``, re-raise.
    - Always unregister the engine controller (cleanup).
    """

    def __init__(
        self,
        *,
        node_registry: NodeRegistry,
        state_registry: StateRegistry,
        spec_store: GraphSpecStore,
        instance_store: GraphInstanceStore,
        checkpoint_store: CheckpointStore,
        deliver_store: DeliverStore | None = None,
    ) -> None:
        """Initialize the orchestrator with the required registries + stores.

        Args:
            node_registry: pre-populated by the bot factory with
                ``AgentNodeFactory``, ``FunctionNodeFactory``, etc.
            state_registry: pre-populated by the bot factory with
                ``ReactStateFactory``, ``SimpleStateFactory``, etc.
            spec_store: persistence for ``GraphSpec`` records.
            instance_store: persistence for ``GraphInstance`` records.
            checkpoint_store: persistence for checkpoint data (recovery).
            deliver_store: persistence for external delivers. If ``None``,
                an ``InMemoryDeliverStore`` is created (suitable for
                single-process runs and tests).
        """
        self._node_registry = node_registry
        self._state_registry = state_registry
        self._spec_store = spec_store
        self._instance_store = instance_store
        self._checkpoint_store = checkpoint_store
        self._deliver_store = deliver_store if deliver_store is not None else InMemoryDeliverStore()
        self._compiler = GraphSpecCompiler(node_registry, state_registry)
        self._runtime = GraphRuntime()

        # Wire recovery + control (rule 15: single control + recovery path).
        # The adapter lets GraphRecoveryService call back into the orchestrator
        # to create engines for recovered instances.
        self._engine_factory = _EngineFactoryAdapter(self)
        self._recovery_service = GraphRecoveryService(
            instance_store,
            checkpoint_store,
            self._engine_factory,
        )
        self._control_service = GraphControlService(
            instance_store,
            self._deliver_store,
            self._recovery_service,
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
        instance = GraphInstance(
            graph_instance_id=graph_instance_id,
            spec_id=spec_id,
            status=GraphInstanceStatus.RUNNING,
            parent_instance_id=parent_instance_id,
        )
        self._instance_store.save(instance)
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
        """Resume a paused/stopped graph instance.

        Delegates to ``GraphControlService`` → ``GraphRecoveryService.resume``.
        The recovery service validates the status (PAUSED/STOPPED only),
        sets it to RUNNING, and calls ``_run_existing_instance`` to re-run
        the graph from the latest checkpoint.
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

    # ── Internal: recovery path (called by _EngineFactoryAdapter) ───────

    async def _run_existing_instance(self, instance: GraphInstance) -> None:
        """Recovery path: load spec, compile, run with an existing instance.

        Called by ``_EngineFactoryAdapter.create_and_run`` when
        ``GraphRecoveryService`` recovers a crashed/paused/stopped instance.
        The scheduler restores state from checkpoint inside ``run_async``.

        A fresh default state is created here — the scheduler overwrites it
        from the checkpoint at the start of ``run_async`` (if a checkpoint
        exists). If no checkpoint exists (rare for recovery — an instance
        that crashed before the first checkpoint), the fresh state is used.
        """
        spec = self._load_spec(instance.spec_id)
        compiled = self._compiler.compile(spec)
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

        Lifecycle transitions (ticket 10 §3.4):

        - Normal completion → ``COMPLETED``.
        - ``GraphInterrupt`` (HITL suspend) → ``PAUSED``, re-raise.
        - Any other exception → ``CRASHED``, re-raise.
        - Always unregister the engine controller (cleanup).
        """
        gid = instance.graph_instance_id
        engine: GraphEngine[Any] = GraphEngine(
            compiled, checkpoint_store=self._checkpoint_store
        )
        ctx: GraphContext[Any] = GraphContext(
            state=state,
            runtime=self._runtime,
            graph_instance_id=gid,
        )
        controller: GraphEngineController = InMemoryGraphEngineController(gid)
        self._control_service.register_engine(controller)
        try:
            await engine.run_async(ctx)
            self._instance_store.update_status(gid, GraphInstanceStatus.COMPLETED.value)
        except GraphInterrupt:
            self._instance_store.update_status(gid, GraphInstanceStatus.PAUSED.value)
            raise
        except Exception:
            self._instance_store.update_status(gid, GraphInstanceStatus.CRASHED.value)
            raise
        finally:
            self._control_service.unregister_engine(gid)

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
        """Create a fresh ``GraphState`` from the spec's ``state_schema``.

        - Inline ``StateSchema`` → ``DynamicStateFactory(schema).create_state()``.
        - Registered name (``str``) → ``state_registry.create_state(name)``.

        Mirrors ``GraphSpecCompiler._resolve_state_factory`` — the compiler
        resolves the factory for validation only (no state created); the
        orchestrator creates the actual state here.
        """
        schema = spec.state_schema
        if isinstance(schema, StateSchema):
            return DynamicStateFactory(schema).create_state()
        return self._state_registry.create_state(schema)

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
