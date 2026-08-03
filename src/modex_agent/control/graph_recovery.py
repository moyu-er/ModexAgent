"""`GraphRecoveryService` — recovery for graph instances (ticket 10 §3.5).

Two recovery types share the SAME flow (rule 15: converge — single
recovery path, no per-type branches):

- **Fault recovery** (`recover_crashed`) — auto-pick `CRASHED` instances
  on startup, reload from checkpoint, re-dispatch.
- **Manual recovery** (`resume`) — reload a `PAUSED`/`STOPPED` instance
  on external `resume()`.

The shared per-instance flow is:

1. Set status to `RUNNING` (in `GraphInstanceStore`).
2. Call `engine_factory.create_and_run(instance)` — the factory creates
   a `GraphEngine` + `ParallelScheduler`, passes `graph_instance_id`
   via `ctx`, and the scheduler's `run_async` calls
   `checkpoint_store.load_latest → _restore_from_checkpoint →
   re-dispatch`.

`GraphEngineFactory` is an ABC (rule 7) because actual engine creation
depends on business wiring (`NodeFactory`, `StateFactory`, etc.) which
lives in the bot factory (P3.5). It is the second consumer of
`GraphInstance` after `GraphSpecCompiler` — a real seam (rule 6).

`checkpoint_store` is held by the recovery service so it is the
checkpoint-aware owner of the recovery flow. The actual checkpoint
loading happens INSIDE `ParallelScheduler.run_async` (called by the
engine factory) — the recovery service triggers the engine, and the
engine rebuilds state from the checkpoint.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from modex_graph import (
    CheckpointStore,
    GraphInstance,
    GraphInstanceStatus,
    GraphInstanceStore,
)

logger = logging.getLogger(__name__)


class GraphEngineFactory(ABC):
    """ABC for creating and running a `GraphEngine` for a recovered instance.

    The factory is the business-wiring seam (rule 6: the second consumer
    of `GraphInstance` after `GraphSpecCompiler` — a real seam, not a
    hypothetical one). The actual engine creation depends on
    `NodeFactory`, `StateFactory`, `GraphSpecStore`, etc., which live in
    the bot factory (P3.5). The framework provides the contract; the bot
    factory provides the implementation.

    The implementation must:

    - Load the `GraphSpec` from `instance.spec_id` via `GraphSpecStore`.
    - Compile it via `GraphSpecCompiler` to get a `CompiledGraph`.
    - Construct a `GraphEngine` with a `ParallelScheduler` wired to the
      same `CheckpointStore` that the recovery service holds.
    - Call `engine.run(graph_instance_id=instance.graph_instance_id, …)`
      so the scheduler's `run_async` loads the latest checkpoint and
      restores state.
    """

    @abstractmethod
    async def create_and_run(self, instance: GraphInstance) -> None:
        """Create a `GraphEngine` for the instance and run it.

        Recovery = `run_async` with the existing `graph_instance_id`.
        The scheduler's `run_async` calls `load_latest` at the top; if a
        checkpoint exists, state is rebuilt via
        `_restore_from_checkpoint` and pending dispatches are
        re-dispatched. If no checkpoint exists, fresh start (rare for
        recovery — typically only crashed instances that never reached
        the first checkpoint).

        Args:
            instance: The `GraphInstance` to recover. Its
                `graph_instance_id` is the persistence key for the
                checkpoint.
        """
        ...


class GraphRecoveryService:
    """Recovery service for graph instances (ticket 10 §3.5).

    Two recovery types sharing the same flow (rule 15: converge —
    single recovery path for both auto and manual):

    - **Fault recovery** (`recover_crashed`) — auto-pick `CRASHED`
      instances on startup, reload from checkpoint, re-dispatch.
    - **Manual recovery** (`resume`) — reload a `PAUSED`/`STOPPED`
      instance on external `resume()`.

    The only difference between the two types is the trigger condition
    and the status filter. The per-instance recovery flow is identical:
    set status to `RUNNING` → call `engine_factory.create_and_run`.

    `checkpoint_store` is held so the recovery service is the
    checkpoint-aware owner of the recovery flow. The actual checkpoint
    loading is delegated to `ParallelScheduler.run_async` inside the
    engine factory's `create_and_run` call.
    """

    def __init__(
        self,
        instance_store: GraphInstanceStore,
        checkpoint_store: CheckpointStore,
        engine_factory: GraphEngineFactory,
    ) -> None:
        self._instance_store = instance_store
        self._checkpoint_store = checkpoint_store
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
        crashed = self._instance_store.load_by_status(
            GraphInstanceStatus.CRASHED.value
        )
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
        instance = self._instance_store.load_by_id(graph_instance_id)
        if instance is None:
            raise ValueError(
                f"Graph instance {graph_instance_id} not found; "
                "cannot resume"
            )
        if instance.status not in (
            GraphInstanceStatus.PAUSED.value,
            GraphInstanceStatus.STOPPED.value,
        ):
            raise ValueError(
                f"Graph instance {graph_instance_id} status is "
                f"{instance.status!r}; only PAUSED/STOPPED can be "
                "manually resumed"
            )
        await self._recover_instances([instance])

    async def _recover_instances(
        self, instances: list[GraphInstance]
    ) -> list[int]:
        """Shared recovery flow (rule 15: single path for both types).

        For each instance:

        1. Set status to `RUNNING` (in `GraphInstanceStore`).
        2. Call `engine_factory.create_and_run(instance)` — the engine
           loads the latest checkpoint via `run_async` and re-dispatches.

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
