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
  pause / stop the engine and deliver content to a node.

`GraphEngineController` is the ABC (rule 7) for the engine handle.
`LiveGraphEngineController` connects commands to a running graph's
`GraphRunControl`. `InMemoryGraphEngineController` remains a recording stub
for tests and fallback scenarios.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from modex_agent.control.graph_recovery import GraphRecoveryService
from modex_agent.control.types import ControlCommand, ControlCommandType
from modex_graph import (
    GraphInstanceStatus,
    GraphInstanceStore,
    GraphPersistenceCoordinator,
    GraphRunControl,
    RoutingError,
)

logger = logging.getLogger(__name__)


class GraphEngineController(ABC):
    """ABC for controlling a running graph engine (rule 7).

    The controller is the in-memory handle to a running graph engine,
    keyed by `graph_instance_id`. It exposes the three operations that
    `GraphControlService` routes to via `ControlCommand`:

    - `pause()` — signal the engine to stop scheduling new nodes.
    - `stop()` — cancel the running engine.
    - `deliver_to_node(node_name, content)` — notify the engine that
      content was externally delivered to a node.

    `LiveGraphEngineController` controls a running scheduler.
    `InMemoryGraphEngineController` is a recording stub.
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
    async def deliver_to_node(self, node_name: str, content: Any) -> None:
        """Notify the engine of an external deliver to a node."""
        ...


class InMemoryGraphEngineController(GraphEngineController):
    """In-memory recording stub controller.

    Records `pause` / `stop` / `deliver_to_node` calls for verification.
    """

    def __init__(self, graph_instance_id: int) -> None:
        self._graph_instance_id = graph_instance_id
        self.pause_called: bool = False
        self.stop_called: bool = False
        self.deliver_calls: list[tuple[str, Any]] = []

    @property
    def graph_instance_id(self) -> int:
        return self._graph_instance_id

    async def pause(self) -> None:
        self.pause_called = True

    async def stop(self) -> None:
        self.stop_called = True

    async def deliver_to_node(self, node_name: str, content: Any) -> None:
        self.deliver_calls.append((node_name, content))


class LiveGraphEngineController(GraphEngineController):
    """Connect external graph commands to one running scheduler control."""

    def __init__(self, graph_instance_id: int, control: GraphRunControl) -> None:
        self._graph_instance_id = graph_instance_id
        self._control = control

    @property
    def graph_instance_id(self) -> int:
        return self._graph_instance_id

    async def pause(self) -> None:
        self._control.request_pause("external pause")

    async def stop(self) -> None:
        self._control.request_stop("external stop")

    async def deliver_to_node(self, node_name: str, content: Any) -> None:
        self._control.notify_deliver(node_name)


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
        *,
        finalize_instance: Callable[[int, GraphInstanceStatus], Awaitable[None]] | None = None,
    ) -> None:
        self._instance_store = instance_store
        self._recovery_service = recovery_service
        self._coordinator_lookup = coordinator_lookup
        self._finalize_instance = finalize_instance
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
        gid: int | None = command.scope.graph_instance_id
        if gid is None:
            raise ValueError(
                f"Graph control command {command.command_id} "
                f"({command.type.value}) requires scope.graph_instance_id"
            )
        return gid

    async def _pause(self, command: ControlCommand) -> None:
        gid = self._require_graph_instance_id(command)
        metadata = self._instance_store.load(gid)
        if metadata is None:
            raise ValueError(f"Graph instance {gid} not found")
        if metadata.status != GraphInstanceStatus.RUNNING:
            raise ValueError(
                f"Cannot pause instance {gid}: status is "
                f"{metadata.status.value}, must be RUNNING"
            )
        self._instance_store.update_status(gid, GraphInstanceStatus.PAUSED)
        engine = self._engines.get(gid)
        if engine is not None:
            await engine.pause()

    async def _stop(self, command: ControlCommand) -> None:
        gid = self._require_graph_instance_id(command)
        metadata = self._instance_store.load(gid)
        if metadata is None:
            raise ValueError(f"Graph instance {gid} not found")
        if metadata.status not in {
            GraphInstanceStatus.RUNNING,
            GraphInstanceStatus.PAUSED,
        }:
            raise ValueError(
                f"Cannot stop instance {gid}: status is "
                f"{metadata.status.value}, must be RUNNING or PAUSED"
            )
        self._instance_store.update_status(gid, GraphInstanceStatus.STOPPED)
        engine = self._engines.get(gid)
        if engine is not None:
            await engine.stop()
        elif self._finalize_instance is not None:
            await self._finalize_instance(gid, GraphInstanceStatus.STOPPED)

    async def _resume(self, command: ControlCommand) -> None:
        # Delegate to the recovery service (load checkpoint → rebuild →
        # re-dispatch via orchestrator._run_existing_instance). The recovery
        # service owns the full flow: status validation (PAUSED only —
        # STOPPED is terminal), RUNNING transition, and engine creation.
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
        metadata = self._instance_store.load(gid)
        if metadata is None:
            raise ValueError(f"Graph instance {gid} not found")
        if metadata.status not in {GraphInstanceStatus.RUNNING, GraphInstanceStatus.PAUSED, GraphInstanceStatus.PENDING}:
            raise ValueError(
                f"Cannot deliver to instance {gid}: status is "
                f"{metadata.status.value}, must be RUNNING, PAUSED, or PENDING"
            )
        if node_name not in metadata.node_id_map:
            raise RoutingError(f"Node {node_name!r} has no deliver_store")
        target_node_id = metadata.node_id_map[node_name]
        coordinator.route_deliver(
            target_node_id=target_node_id,
            content=content,
            source_node_id="__external__",
            source_invocation_id=0,
            stage=False,
        )
        engine = self._engines.get(gid)
        if engine is not None:
            await engine.deliver_to_node(node_name, content)


__all__ = [
    "GraphEngineController",
    "InMemoryGraphEngineController",
    "LiveGraphEngineController",
    "GraphControlService",
]
