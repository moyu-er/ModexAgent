"""`GraphRecoveryService` — recovery for graph instances.

Two recovery types share the SAME flow (rule 15: converge — single
recovery path, no per-type branches):

- **Fault recovery** (`recover_crashed`) — auto-pick `CRASHED` instances
  on startup, reload via coordinator, re-dispatch.
- **Manual recovery** (`resume`) — reload a `PAUSED`/`STOPPED` instance
  on external `resume()`.

The shared per-instance flow is:

1. Load `GraphMetadata` from `GraphInstanceStore`.
2. Reconstruct the coordinator (`create_null_coordinator(gid)` —
   SQLite strategy would recover state from DB; Null is the current default).
3. Create `GraphInstance(metadata, coordinator)`.
4. Set status to `RUNNING` (in `GraphInstanceStore`).
5. Call `engine_factory.create_and_run(instance)` — the factory (wired to
   `GraphOrchestrator._run_existing_instance` via `_EngineFactoryAdapter`)
   handles eviction, node registration, registry insertion, and
   `_execute`. The scheduler's `run_async` calls
   `coordinator.load_for_recovery → _restore_from_recovery → re-dispatch`.

`GraphEngineFactory` is an ABC (rule 7) because actual engine creation
depends on business wiring (`NodeFactory`, `StateFactory`, etc.) which
lives in the bot factory. It is the second consumer of
`GraphInstance` after `GraphSpecCompiler` — a real seam (rule 6).

Recovery state loading happens INSIDE `ParallelScheduler.run_async`
(called by the engine factory) — the scheduler calls
`ctx.coordinator.load_for_recovery()` at the top of `run_async`, which
returns a `RecoveryContext` with metadata + node_states + rebuilt
main_state. The scheduler rebuilds its in-memory state from this context.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from modex_graph import (
    GraphInstance,
    GraphInstanceStatus,
    GraphInstanceStore,
    create_null_coordinator,
)

logger = logging.getLogger(__name__)


class GraphEngineFactory(ABC):
    """ABC for creating and running a `GraphEngine` for a recovered instance.

    The factory is the business-wiring seam (rule 6: the second consumer
    of `GraphInstance` after `GraphSpecCompiler` — a real seam, not a
    hypothetical one). The actual engine creation depends on
    `NodeFactory`, `StateFactory`, `GraphSpecStore`, etc., which live in
    the bot factory. The framework provides the contract; the bot
    factory provides the implementation.

    The implementation must:

    - Load the `GraphSpec` from `instance.spec_id` via `GraphSpecStore`.
    - Compile it via `GraphSpecCompiler` to get a `CompiledGraph`.
    - Construct a `GraphEngine` and a `GraphContext` with a coordinator
      wired to the same persistence stores used in the original run.
    - Call `engine.run_async(ctx)` so the scheduler's `run_async` calls
      `coordinator.load_for_recovery()` and restores state.
    """

    @abstractmethod
    async def create_and_run(self, instance: GraphInstance) -> None:
        """Create a `GraphEngine` for the instance and run it.

        Recovery = `run_async` with the existing `graph_instance_id`.
        The scheduler's `run_async` calls `coordinator.load_for_recovery()`
        at the top; if prior state exists, state is rebuilt via
        `_restore_from_recovery` and pending dispatches are re-dispatched.
        If no prior state exists, fresh start.

        Args:
            instance: The `GraphInstance` to recover. Its
                `graph_instance_id` is the persistence key.
        """
        ...


class GraphRecoveryService:
    """Recovery service for graph instances.

    Two recovery types sharing the same flow (rule 15: converge —
    single recovery path for both auto and manual):

    - **Fault recovery** (`recover_crashed`) — auto-pick `CRASHED`
      instances on startup, reload via coordinator, re-dispatch.
    - **Manual recovery** (`resume`) — reload a `PAUSED`/`STOPPED`
      instance on external `resume()`.

    The only difference between the two types is the trigger condition
    and the status filter. The per-instance recovery flow is identical:
    load metadata → reconstruct coordinator → create GraphInstance →
    set status to `RUNNING` → call `engine_factory.create_and_run`.

    The engine factory (wired to `GraphOrchestrator._run_existing_instance`
    via `_EngineFactoryAdapter`) handles eviction, node registration,
    registry insertion, and `_execute`. Recovery state loading is
    delegated to `ParallelScheduler.run_async` inside the engine factory's
    `create_and_run` call, which calls `coordinator.load_for_recovery()`.
    """

    def __init__(
        self,
        instance_store: GraphInstanceStore,
        engine_factory: GraphEngineFactory,
    ) -> None:
        self._instance_store = instance_store
        self._engine_factory = engine_factory

    async def recover_crashed(self) -> list[int]:
        """Fault recovery: find all `CRASHED` instances, reload, re-dispatch.

        Called on startup (or on-demand by an operator) to auto-recover
        instances that crashed mid-execution. Only `CRASHED` instances
        are picked up — `PAUSED`/`STOPPED` are NOT auto-recovered (they
        require explicit `resume()`); `COMPLETED`/`FAILED` are terminal.

        Returns:
            The list of recovered `graph_instance_id`s. Callers (e.g.
            the bot factory) may log or expose this for observability.
        """
        crashed_metadata = self._instance_store.load_by_status(
            GraphInstanceStatus.CRASHED.value
        )
        crashed = [
            GraphInstance(m, create_null_coordinator(m.graph_instance_id))
            for m in crashed_metadata
        ]
        return await self._recover_instances(crashed)

    async def resume(self, graph_instance_id: int) -> None:
        """Manual recovery: reload a `PAUSED`/`STOPPED` instance, re-dispatch.

        Triggered by external `resume()` (REST/CLI via
        `GraphControlService`). Only `PAUSED`/`STOPPED` instances can be
        manually resumed — `CRASHED` instances are auto-recovered by
        `recover_crashed`, and `COMPLETED`/`FAILED` are terminal.

        Args:
            graph_instance_id: The instance to resume.

        Raises:
            ValueError: If the instance does not exist, or its status
                is not `PAUSED`/`STOPPED`.
        """
        metadata = self._instance_store.load_by_id(graph_instance_id)
        if metadata is None:
            raise ValueError(
                f"Graph instance {graph_instance_id} not found; "
                "cannot resume"
            )
        if metadata.status not in (
            GraphInstanceStatus.PAUSED.value,
            GraphInstanceStatus.STOPPED.value,
        ):
            raise ValueError(
                f"Graph instance {graph_instance_id} status is "
                f"{metadata.status!r}; only PAUSED/STOPPED can be "
                "manually resumed"
            )
        instance = GraphInstance(metadata, create_null_coordinator(graph_instance_id))
        await self._recover_instances([instance])

    async def _recover_instances(
        self, instances: list[GraphInstance]
    ) -> list[int]:
        """Shared recovery flow (rule 15: single path for both types).

        For each instance (already constructed with a reconstructed
        coordinator via ``create_null_coordinator``):

        1. Set status to `RUNNING` (in `GraphInstanceStore`).
        2. Call `engine_factory.create_and_run(instance)` — the factory
           (wired to ``GraphOrchestrator._run_existing_instance``) handles
           eviction, node registration, registry insertion, and
           `_execute`. The engine loads recovery state via
           `coordinator.load_for_recovery()` and re-dispatches.

        Returns the list of recovered `graph_instance_id`s.
        """
        recovered: list[int] = []
        for instance in instances:
            self._instance_store.update_status(
                instance.graph_instance_id,
                GraphInstanceStatus.RUNNING.value,
            )
            await self._engine_factory.create_and_run(instance)
            recovered.append(instance.graph_instance_id)
        return recovered


__all__ = [
    "GraphEngineFactory",
    "GraphRecoveryService",
]
