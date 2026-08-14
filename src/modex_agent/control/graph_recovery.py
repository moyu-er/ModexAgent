"""`GraphRecoveryService` — recovery for graph instances.

Two recovery types share the SAME flow (rule 15: converge — single
recovery path, no per-type branches):

- **Fault recovery** (`recover_crashed`) — auto-pick `CRASHED` instances
  on startup, reload via coordinator, re-dispatch.
- **Manual recovery** (`resume`) — reload a `PAUSED` instance on
  external `resume()`. `STOPPED` is terminal (manual termination) and
  cannot be resumed.

The shared per-instance flow is:

1. Load `GraphMetadata` from `GraphInstanceStore`.
2. Reconstruct the coordinator via `coordinator_factory.create(gid,
   instance_store)` — the factory assembles the node state store and
   deliver store factory internally. The framework default is
   `NullCoordinatorFactory` (no-op persistence); a SQLite-backed factory
   would recover state from DB.
3. Create `GraphInstance(metadata, coordinator)`.
4. Set status to `RUNNING` (in `GraphInstanceStore`).
5. Call `orchestrator._run_existing_instance(instance)` directly — the
   orchestrator handles eviction, spec compile, node_id restore, node
   registration, registry insertion, and `run_instance`. The scheduler's
   `run_async` calls `bootstrap(ctx, graph)` to derive seed nodes from
   persisted state.

`GraphRecoveryService` holds a direct reference to the
`GraphOrchestrator` rather than going through an adapter ABC — recovery
is a single internal call path (rule 15: converge), not a pluggable
seam. The orchestrator's `_run_existing_instance` performs the 7-step
re-registration before the scheduler takes over normal scheduling.

Recovery state loading happens INSIDE the scheduler's `run_async` (called
by `run_instance`) — the scheduler calls `bootstrap(ctx, graph)` at the
top of `run_async`, which queries the persistence store and derives seed
nodes (non-terminal invocations + nodes with PENDING delivers). The
scheduler rebuilds its in-memory state from these seeds.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import TYPE_CHECKING

from modex_graph import (
    CoordinatorFactory,
    GraphInstance,
    GraphInstanceStatus,
    GraphInstanceStore,
    GraphInterrupt,
)

if TYPE_CHECKING:
    from modex_agent.orchestration.graph_orchestrator import GraphOrchestrator

logger = logging.getLogger(__name__)


class GraphRecoveryService:
    """Recovery service for graph instances.

    Two recovery types sharing the same flow (rule 15: converge —
    single recovery path for both auto and manual):

    - **Fault recovery** (`recover_crashed`) — auto-pick `CRASHED`
      instances on startup, reload via coordinator, re-dispatch.
    - **Manual recovery** (`resume`) — reload a `PAUSED` instance on
      external `resume()`. `STOPPED` is terminal (manual termination)
      and cannot be resumed; `CRASHED` is recovered via
      `recover_crashed()`; `COMPLETED`/`FAILED` are terminal.

    The only difference between the two types is the trigger condition
    and the status filter. The per-instance recovery flow is identical:
    load metadata → reconstruct coordinator → create GraphInstance →
    set status to `RUNNING` → call `orchestrator._run_existing_instance`.

    The orchestrator's `_run_existing_instance` handles eviction, node
    registration, registry insertion, and `run_instance`. Recovery state
    loading is delegated to the scheduler's `run_async` inside
    `run_instance`, which calls `bootstrap(ctx, graph)`.
    """

    def __init__(
        self,
        instance_store: GraphInstanceStore,
        orchestrator: GraphOrchestrator,
        *,
        coordinator_factory: CoordinatorFactory,
    ) -> None:
        self._instance_store = instance_store
        self._orchestrator = orchestrator
        self._coordinator_factory = coordinator_factory

    async def recover_crashed(self) -> list[int]:
        """Fault recovery: find all non-terminal crashed instances, reload, re-dispatch.

        Called on startup (or on-demand by an operator) to auto-recover
        instances that crashed mid-execution. Picks up both explicit
        ``CRASHED`` instances and orphan ``RUNNING`` instances — a process
        kill leaves the graph in ``RUNNING`` because the in-process
        exception handler never runs.

        ``PAUSED`` is NOT auto-recovered (requires explicit ``resume()``);
        ``STOPPED``/``COMPLETED``/``FAILED`` are terminal.

        Returns:
            The list of recovered ``graph_instance_id``s. Callers (e.g.
            the bot factory) may log or expose this for observability.
        """
        crashed = self._instance_store.load_by_status(
            GraphInstanceStatus.CRASHED
        )
        orphaned = self._instance_store.load_by_status(
            GraphInstanceStatus.RUNNING
        )
        instances = [
            GraphInstance(
                m,
                self._coordinator_factory.create(m.graph_instance_id, self._instance_store),
            )
            for m in crashed + orphaned
        ]
        return await self._recover_instances(instances)

    async def resume(self, graph_instance_id: int) -> None:
        """Manual recovery: reload a `PAUSED` instance, re-dispatch.

        Triggered by external `resume()` (REST/CLI via
        `GraphControlService`). Only `PAUSED` instances can be manually
        resumed — `STOPPED` is a terminal status (manual termination)
        and cannot be resumed; `CRASHED` instances are auto-recovered by
        `recover_crashed()`; `COMPLETED`/`FAILED` are terminal;
        `RUNNING` means the instance is already active.

        Args:
            graph_instance_id: The instance to resume.

        Raises:
            ValueError: If the instance does not exist, or its status
                is not `PAUSED`.
        """
        metadata = self._instance_store.load(graph_instance_id)
        if metadata is None:
            raise ValueError(
                f"Graph instance {graph_instance_id} not found; "
                "cannot resume"
            )
        if metadata.status == GraphInstanceStatus.STOPPED:
            raise ValueError(
                f"Cannot resume instance {graph_instance_id}: "
                f"STOPPED is a terminal status (manual termination); "
                "only PAUSED instances can be resumed."
            )
        if metadata.status != GraphInstanceStatus.PAUSED:
            raise ValueError(
                f"Cannot resume instance {graph_instance_id}: "
                f"status is {metadata.status!r}; only PAUSED instances "
                "can be resumed (STOPPED/COMPLETED/FAILED are terminal, "
                "CRASHED is auto-recovered via recover_crashed(), "
                "RUNNING means already active)."
            )
        instance = GraphInstance(
            metadata,
            self._coordinator_factory.create(graph_instance_id, self._instance_store),
        )
        await self._recover_instances([instance])

    async def _recover_instances(
        self, instances: list[GraphInstance]
    ) -> list[int]:
        """Shared recovery flow (rule 15: single path for both types).

        For each instance (already constructed with a reconstructed
        coordinator via ``coordinator_factory.create``):

        1. Set status to `RUNNING` (in `GraphInstanceStore`).
        2. Call `orchestrator._run_existing_instance(instance)` — the
           orchestrator handles eviction, spec compile, node_id
           restore, node registration, registry insertion, and
           `run_instance`. The scheduler loads recovery state via
           `bootstrap(ctx, graph)` and re-dispatches.

        Per-instance failures are isolated: if one instance raises, the
        remaining are still attempted. Failed instances are left in
        CRASHED status (set by the orchestrator's ``except Exception``
        handler) and are NOT included in the returned list.

        Returns the list of successfully recovered ``graph_instance_id``s.
        """
        recovered: list[int] = []
        for instance in instances:
            try:
                self._instance_store.update_status(
                    instance.graph_instance_id,
                    GraphInstanceStatus.RUNNING,
                )
                await self._orchestrator._run_existing_instance(instance)
                recovered.append(instance.graph_instance_id)
            except GraphInterrupt:
                raise
            except Exception:
                logger.exception(
                    "Recovery failed for graph instance %s; "
                    "continuing to next candidate",
                    instance.graph_instance_id,
                )
                with suppress(Exception):
                    self._instance_store.update_status(
                        instance.graph_instance_id,
                        GraphInstanceStatus.CRASHED,
                    )
        return recovered


__all__ = [
    "GraphRecoveryService",
]
