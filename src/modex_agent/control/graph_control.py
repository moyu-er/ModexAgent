# ruff: noqa: ANN401

"""`GraphControlService` — external control interface for graph instances (ticket 10 §3.3).

External control (pause / stop / resume / deliver) all go through the same
`ControlCommand` pattern (rule 15: converge — single control path). REST +
CLI converge to this service.

The service holds:

- `instance_store: GraphInstanceStore` — for status persistence (running →
  paused / stopped / running transitions).
- `deliver_store: DeliverStore` — for `DELIVER_TO_NODE` accumulation.
  External delivers are persisted so they survive crashes and are picked
  up when the node's `_collect_delivers` next runs.
- `engines: dict[int, GraphEngineController]` — running engine handles,
  keyed by `graph_instance_id`. The handle is a lightweight ABC that can
  pause / stop / resume the engine and deliver content to a node.

`GraphEngineController` is the ABC (rule 7) for the engine handle.
`InMemoryGraphEngineController` is a recording stub — it sets boolean
flags but does NOT actually control a running `ParallelScheduler` loop.
A `LiveGraphEngineController` that wires pause/stop/resume into the
scheduler loop is deferred (not yet specified in the implementation
plan).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from modex_agent.control.graph_recovery import GraphRecoveryService
from modex_agent.control.types import ControlCommand, ControlCommandType
from modex_graph import DeliverStore, GraphInstanceStatus, GraphInstanceStore

logger = logging.getLogger(__name__)


class GraphEngineController(ABC):
    """ABC for controlling a running graph engine (rule 7).

    The controller is the in-memory handle to a running graph engine,
    keyed by `graph_instance_id`. It exposes the four operations that
    `GraphControlService` routes to via `ControlCommand`:

    - `pause()` — signal the engine to stop scheduling new nodes.
    - `stop()` — cancel the running engine.
    - `resume()` — re-dispatch from checkpoint (delegates to P2.6
      recovery).
    - `deliver_to_node(node_name, content)` — notify the engine that
      content was externally delivered to a node.

    For P2.5, `InMemoryGraphEngineController` is a recording stub. A
    `LiveGraphEngineController` that wires pause/stop/resume into the
    scheduler loop is deferred (not yet specified in the implementation
    plan).
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
        """Re-dispatch from checkpoint (delegates to P2.6 recovery)."""
        ...

    @abstractmethod
    async def deliver_to_node(self, node_name: str, content: Any) -> None:
        """Notify the engine of an external deliver to a node."""
        ...


class InMemoryGraphEngineController(GraphEngineController):
    """In-memory recording stub controller.

    Records `pause` / `stop` / `resume` / `deliver_to_node` calls for
    verification. A `LiveGraphEngineController` that wires pause/stop/
    resume into the scheduler loop is deferred (not yet specified in
    the implementation plan).
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
    """Routes `ControlCommand`s to graph instance actions (ticket 10 §3.3).

    External control (pause / stop / resume / deliver) all go through the
    same `ControlCommand` pattern (rule 15: converge — single control
    path). REST + CLI converge to this service.

    The service persists status transitions via `GraphInstanceStore` and
    accumulates external delivers via `DeliverStore`. Running engines are
    notified via `GraphEngineController` handles registered by the bot
    factory.
    """

    def __init__(
        self,
        instance_store: GraphInstanceStore,
        deliver_store: DeliverStore,
        recovery_service: GraphRecoveryService | None = None,
    ) -> None:
        self._instance_store = instance_store
        self._deliver_store = deliver_store
        self._recovery_service = recovery_service
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
        gid = self._require_graph_instance_id(command)
        if self._recovery_service is not None:
            # P2.6: delegate to the recovery service (load checkpoint →
            # rebuild → re-dispatch via engine_factory.create_and_run).
            # The recovery service owns the full flow: status validation,
            # RUNNING transition, and engine creation.
            await self._recovery_service.resume(gid)
            return
        # P2.5 in-memory mode: no recovery service wired. Status
        # transition + engine.resume() on the registered controller
        # (stub). The bot factory (P3.5) wires the recovery service.
        self._instance_store.update_status(gid, GraphInstanceStatus.RUNNING)
        engine = self._engines.get(gid)
        if engine is not None:
            await engine.resume()

    async def _deliver(self, command: ControlCommand) -> None:
        gid = self._require_graph_instance_id(command)
        node_name = command.payload.get("node_name")
        if not isinstance(node_name, str):
            raise ValueError(
                f"DELIVER_TO_NODE command {command.command_id} requires "
                "payload['node_name'] to be a str"
            )
        content = command.payload.get("content")
        # Persist via DeliverStore — next_node="" marks the downstream
        # target as unresolved (the node resolves it when it submits).
        self._deliver_store.accumulate(
            graph_instance_id=gid,
            node_name=node_name,
            next_node="",
            content=content,
        )
        engine = self._engines.get(gid)
        if engine is not None:
            await engine.deliver_to_node(node_name, content)


__all__ = [
    "GraphEngineController",
    "InMemoryGraphEngineController",
    "GraphControlService",
]
