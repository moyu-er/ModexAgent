# ruff: noqa: ANN401

"""`GraphControlService` — external control interface for graph instances.

External control (pause / stop / resume / deliver) all go through the same
`ControlCommand` pattern (rule 15: converge — single control path). REST +
CLI converge to this service.

The service holds:

- `instance_store: GraphInstanceStore` — for status persistence (running →
  paused / stopped / running transitions).
- `coordinator_lookup: Callable[[int], GraphPersistenceCoordinator | None]`
  — fetches the coordinator for an active graph instance from the
  orchestrator's `_active_instances` registry. Used by `_deliver` to route
  external delivers through `coordinator.route_deliver` (no shared
  `deliver_store` — delivers go to the per-node store inside the
  coordinator).
- `engines: dict[int, GraphEngineController]` — running engine handles,
  keyed by `graph_instance_id`. The handle is a lightweight ABC that can
  pause / stop / resume the engine and deliver content to a node.

`GraphEngineController` is the ABC (rule 7) for the engine handle.
`InMemoryGraphEngineController` is a recording stub — it sets boolean
flags but does NOT actually control a running `ParallelScheduler` loop.
A `LiveGraphEngineController` that wires pause/stop/resume into the
scheduler loop is deferred (not yet specified).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from modex_agent.control.graph_recovery import GraphRecoveryService
from modex_agent.control.types import ControlCommand, ControlCommandType
from modex_graph import GraphInstanceStatus, GraphInstanceStore, GraphPersistenceCoordinator

logger = logging.getLogger(__name__)


class GraphEngineController(ABC):
    """ABC for controlling a running graph engine (rule 7).

    The controller is the in-memory handle to a running graph engine,
    keyed by `graph_instance_id`. It exposes the four operations that
    `GraphControlService` routes to via `ControlCommand`:

    - `pause()` — signal the engine to stop scheduling new nodes.
    - `stop()` — cancel the running engine.
    - `resume()` — re-dispatch from checkpoint (delegates to recovery).
    - `deliver_to_node(node_name, content)` — notify the engine that
      content was externally delivered to a node.

    `InMemoryGraphEngineController` is a recording stub. A
    `LiveGraphEngineController` that wires pause/stop/resume into the
    scheduler loop is deferred (not yet specified).
    """

    @property
    @abstractmethod
    def graph_instance_id(self) -> int:
        """The graph instance this controller manages."""
        ...

    @abstractmethod
    async def pause(self) -> None:
        """Signal the engine to stop scheduling new nodes."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Cancel the running engine."""
        ...

    @abstractmethod
    async def resume(self) -> None:
        """Re-dispatch from checkpoint (delegates to recovery)."""
        ...

    @abstractmethod
    async def deliver_to_node(self, node_name: str, content: Any) -> None:
        """Notify the engine of an external deliver to a node."""
        ...


class InMemoryGraphEngineController(GraphEngineController):
    """In-memory recording stub controller.

    Records `pause` / `stop` / `resume` / `deliver_to_node` calls for
    verification. A `LiveGraphEngineController` that wires pause/stop/
    resume into the scheduler loop is deferred (not yet specified).
    """

    def __init__(self, graph_instance_id: int) -> None:
        self._graph_instance_id = graph_instance_id
        self.pause_called: bool = False
        self.stop_called: bool = False
        self.resume_called: bool = False
        self.deliver_calls: list[tuple[str, Any]] = []

    @property
    def graph_instance_id(self) -> int:
        return self._graph_instance_id

    async def pause(self) -> None:
        self.pause_called = True

    async def stop(self) -> None:
        self.stop_called = True

    async def resume(self) -> None:
        self.resume_called = True

    async def deliver_to_node(self, node_name: str, content: Any) -> None:
        self.deliver_calls.append((node_name, content))


class GraphControlService:
    """Routes `ControlCommand`s to graph instance actions.

    External control (pause / stop / resume / deliver) all go through the
    same `ControlCommand` pattern (rule 15: converge — single control
    path). REST + CLI converge to this service.

    The service persists status transitions via `GraphInstanceStore`.
    External delivers are routed through `coordinator.route_deliver`
    via the `coordinator_lookup` callable — no shared
    `deliver_store`. Running engines are notified via
    `GraphEngineController` handles registered by the orchestrator.
    """

    def __init__(
        self,
        instance_store: GraphInstanceStore,
        recovery_service: GraphRecoveryService,
        coordinator_lookup: Callable[[int], GraphPersistenceCoordinator | None],
    ) -> None:
        self._instance_store = instance_store
        self._recovery_service = recovery_service
        self._coordinator_lookup = coordinator_lookup
        self._engines: dict[int, GraphEngineController] = {}

    def register_engine(self, controller: GraphEngineController) -> None:
        """Register a running engine controller."""
        self._engines[controller.graph_instance_id] = controller

    def unregister_engine(self, graph_instance_id: int) -> None:
        """Unregister an engine controller (e.g. after the graph completes)."""
        self._engines.pop(graph_instance_id, None)

    async def handle(self, command: ControlCommand) -> None:
        """Route a control command to the appropriate graph action.

        Non-graph command types (CANCEL_TURN, INJECT_STEER, etc.) are
        ignored — they are handled by other control-plane consumers.
        """
        match command.type:
            case ControlCommandType.PAUSE_GRAPH:
                await self._pause(command)
            case ControlCommandType.STOP_GRAPH:
                await self._stop(command)
            case ControlCommandType.RESUME_GRAPH:
                await self._resume(command)
            case ControlCommandType.DELIVER_TO_NODE:
                await self._deliver(command)
            case _:
                pass

    @staticmethod
    def _require_graph_instance_id(command: ControlCommand) -> int:
        gid = command.scope.graph_instance_id
        if gid is None:
            raise ValueError(
                f"Graph control command {command.command_id} "
                f"({command.type.value}) requires scope.graph_instance_id"
            )
        return gid

    async def _pause(self, command: ControlCommand) -> None:
        gid = self._require_graph_instance_id(command)
        self._instance_store.update_status(gid, GraphInstanceStatus.PAUSED)
        engine = self._engines.get(gid)
        if engine is not None:
            await engine.pause()

    async def _stop(self, command: ControlCommand) -> None:
        gid = self._require_graph_instance_id(command)
        self._instance_store.update_status(gid, GraphInstanceStatus.STOPPED)
        engine = self._engines.get(gid)
        if engine is not None:
            await engine.stop()

    async def _resume(self, command: ControlCommand) -> None:
        # Delegate to the recovery service (load checkpoint → rebuild →
        # re-dispatch via engine_factory.create_and_run). The recovery
        # service owns the full flow: status validation (PAUSED/STOPPED
        # only), RUNNING transition, and engine creation.
        gid = self._require_graph_instance_id(command)
        await self._recovery_service.resume(gid)

    async def _deliver(self, command: ControlCommand) -> None:
        gid = self._require_graph_instance_id(command)
        node_name = command.payload.get("node_name")
        if not isinstance(node_name, str):
            raise ValueError(
                f"DELIVER_TO_NODE command {command.command_id} requires "
                "payload['node_name'] to be a str"
            )
        content = command.payload.get("content")
        # Route through coordinator.route_deliver (no shared
        # deliver_store). The coordinator holds per-node DeliverStores
        # registered via register_node. source_node="__external__" marks
        # this as an externally-originated deliver (no invocation).
        coordinator = self._coordinator_lookup(gid)
        if coordinator is None:
            raise ValueError(
                f"No active graph instance {gid} for DELIVER_TO_NODE; "
                "instance must be running or paused"
            )
        coordinator.route_deliver(
            target_node=node_name,
            content=content,
            source_node="__external__",
            source_invocation_id=0,
        )
        engine = self._engines.get(gid)
        if engine is not None:
            await engine.deliver_to_node(node_name, content)


__all__ = [
    "GraphEngineController",
    "InMemoryGraphEngineController",
    "GraphControlService",
]
