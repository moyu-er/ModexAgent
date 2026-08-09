# ruff: noqa: ANN401

"""``GraphPersistenceCoordinator`` — persistence routing + recovery.

The coordinator holds:

- ``graph_instance_id`` — the persistence key binding all stores to one run.
- ``instance_store`` — graph-instance-level metadata (identity + status).
- ``node_state_store`` — lifecycle + version chain + CAS authority for node
  invocations (scoped to ``graph_instance_id`` at construction).
- ``deliver_stores: dict[str, DeliverStore]`` — per-node deliver accumulation.

Lifecycle methods (begin / complete / suspend / crash / cancel / finalize)
live on ``NodeStateStore``, NOT on the coordinator. ``Node.run()`` calls
``ctx.node_state_store`` directly. The coordinator's role is:

- **Deliver routing** — ``route_deliver``, ``collect_consumable_delivers``,
  ``mark_delivers_consumed``, ``promote_delivers``.
- **State queries** — ``rebuild_main_state`` and ``get_graph_state`` query the
  store internally.
- **Registration** — ``register_node`` registers deliver stores.

Consumption methods:

- ``collect_consumable_delivers`` — delegate to ``deliver_store.query_consumable``.
- ``mark_delivers_consumed`` — delegate to ``deliver_store.mark_consumed``.
- ``promote_delivers`` — promote ALL CONSUMED_PENDING for the node.

State query methods:

- ``rebuild_main_state`` — for each node, pick the single newest record from
  the union of COMPLETED and suspended RUNNING, ordered by
  ``updated_at DESC, invocation_id DESC``. Merge per-node winners in global
  ``invocation_id`` order.
- ``get_graph_state`` — collect metadata + each node's version history.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from ..constants import (
    DeliverConsumptionStatus,
    GraphInstanceStatus,
    InvocationStatus,
)
from ..exceptions import RoutingError
from ..output_adapter import GraphOutput, GraphOutputAdapter, GraphOutputKind
from .deliver_store import DeliverRecord, DeliverStore, DeliverStoreFactory, NullDeliverStoreFactory
from .graph_metadata import (
    GraphMetadata,
    GraphStateSnapshot,
    NodeInvocationRecord,
)
from .instance_store import GraphInstanceStore, NullGraphInstanceStore
from .node_state_store import NodeStateStore, NullNodeStateStore

logger = logging.getLogger(__name__)


class GraphPersistenceCoordinator:
    """Persistence routing + recovery coordinator.

    The scheduler is unaware of persistence stores; the coordinator holds
    the instance store + node state store + per-node DeliverStore references
    and routes deliver/consumption/recovery calls.

    The coordinator is also the single emission seam for node-level
    ``GraphOutput`` events (``node_started`` / ``node_completed`` /
    ``node_crashed`` / ``deliver_dispatched``): it is the one object
    reachable from both ``Node.run`` (via ``ctx.coordinator``) and
    ``route_deliver``. The adapter is wired post-construction via
    ``set_output_adapter`` by the assembler that owns it.

    Three implementation strategies:

    - **Null** — ``NullGraphInstanceStore`` + ``NullNodeStateStore`` +
      ``NullDeliverStore``. LinearScheduler default; graphs that don't need
      persistence.
    - **Memory** — ``InMemoryGraphInstanceStore`` +
      ``InMemoryNodeStateStore`` + ``InMemoryDeliverStore``.
      Tests; single-process ephemeral graphs.
    - **SQLite** — ``SqliteGraphInstanceStore`` + ``SqliteNodeStateStore`` +
      ``SqliteDeliverStore``. Production; graphs requiring crash recovery.
    """

    def __init__(
        self,
        graph_instance_id: int,
        instance_store: GraphInstanceStore,
        node_state_store: NodeStateStore,
        default_deliver_store_factory: DeliverStoreFactory,
    ) -> None:
        """Initialize the coordinator.

        Args:
            graph_instance_id: The persistence key binding all stores to one run.
            instance_store: Graph-instance-level metadata store.
            node_state_store: Lifecycle + version chain store, scoped to
                ``graph_instance_id`` at construction.
            default_deliver_store_factory: Factory for creating DeliverStore
                when ``register_node`` is called without an explicit
                ``deliver_store``. Required (not Optional) —
                ``NullDeliverStoreFactory`` is the no-persistence default.
        """
        self._graph_instance_id = graph_instance_id
        self._instance_store = instance_store
        self._node_state_store = node_state_store
        self._default_deliver_store_factory = default_deliver_store_factory
        self._deliver_stores: dict[str, DeliverStore] = {}
        self._output_adapter: GraphOutputAdapter | None = None
        self._emit_tasks: set[asyncio.Task[None]] = set()

    @property
    def node_state_store(self) -> NodeStateStore:
        """The node state store (lifecycle + version chain + CAS authority)."""
        return self._node_state_store

    # ── Output events ────────────────────────────────────────────────

    def set_output_adapter(self, adapter: GraphOutputAdapter | None) -> None:
        """Wire the graph output adapter for node-level events.

        Called post-construction by the assembler that owns the adapter
        (``GraphOrchestrator``) — the coordinator is created by a
        ``CoordinatorFactory`` that knows nothing about output adapters.
        ``None`` (the default) disables emission.
        """
        self._output_adapter = adapter

    def emit_output(
        self,
        kind: GraphOutputKind,
        *,
        result: Any = None,
        error: str | None = None,
        node_id: str | None = None,
        node_name: str | None = None,
        invocation_id: int | None = None,
        target_node_id: str | None = None,
    ) -> None:
        """Emit a ``GraphOutput`` event — the single seam for node-level events.

        Sync fire-and-forget: usable from both async (``Node.run``) and sync
        (``route_deliver``) call sites. The adapter's async ``emit`` is
        scheduled on the running event loop; emit failures are logged, never
        raised (log-and-continue — emission must not affect graph execution).
        No-op when no adapter is wired or no event loop is running.
        ``graph_instance_id`` and ``timestamp`` are stamped here so every
        event carries them.
        """
        adapter = self._output_adapter
        if adapter is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        output = GraphOutput(
            kind=kind,
            graph_instance_id=self._graph_instance_id,
            result=result,
            error=error,
            node_id=node_id,
            node_name=node_name,
            invocation_id=invocation_id,
            target_node_id=target_node_id,
            timestamp=time.time_ns() // 1_000_000,
        )
        task = loop.create_task(self._emit_safe(adapter, output))
        self._emit_tasks.add(task)
        task.add_done_callback(self._emit_tasks.discard)

    async def drain_output_events(self) -> None:
        """Await all pending fire-and-forget emit tasks.

        Called by the assembler (``GraphOrchestrator._finalize_instance``)
        before emitting the terminal output, so node-level events reach the
        adapter before ``graph_completed`` / ``graph_crashed`` — the event
        stream stays causally ordered even when nothing in the graph body
        yielded to the event loop.
        """
        if self._emit_tasks:
            await asyncio.gather(*self._emit_tasks, return_exceptions=True)

    async def _emit_safe(self, adapter: GraphOutputAdapter, output: GraphOutput) -> None:
        """Await ``adapter.emit`` with log-and-continue error isolation."""
        try:
            await adapter.emit(output)
        except Exception:
            logger.warning(
                "graph output adapter emit failed for instance %s (kind=%s)",
                self._graph_instance_id,
                output.kind,
                exc_info=True,
            )

    # ── Registration + routing ───────────────────────────────────────

    def register_node(
        self,
        node_id: str,
        deliver_store: DeliverStore | None = None,
    ) -> None:
        """Register a node's deliver store.

        ``None`` for ``deliver_store`` means "use the default factory".

        Args:
            node_id: The node ID to register.
            deliver_store: Optional explicit DeliverStore. None → default factory.
        """
        ds = (
            deliver_store
            if deliver_store is not None
            else self._default_deliver_store_factory.create()
        )
        self._deliver_stores[node_id] = ds

    def get_deliver_store(self, node_id: str) -> DeliverStore | None:
        """Get a node's deliver_store (for external deliver routing queries)."""
        return self._deliver_stores.get(node_id)

    def route_deliver(
        self,
        target_node_id: str,
        content: Any,
        source_node_id: str,
        source_invocation_id: int,
        source_node_name: str | None = None,
    ) -> int | None:
        """Route a deliver to the target node's deliver_store.

        Args:
            target_node_id: The node ID receiving the deliver.
            content: The delivered content (JSON-serializable).
            source_node_id: The delivering node ID.
            source_invocation_id: The deliverer's invocation_id.
            source_node_name: The delivering node's name (for event emission).
                ``None`` for external delivers (no source node).

        Returns:
            The ``deliver_id`` (Snowflake).

        Raises:
            RoutingError: If ``target_node_id`` has no deliver_store registered.
        """
        store = self._deliver_stores.get(target_node_id)
        if store is None:
            raise RoutingError(f"Node {target_node_id!r} has no deliver_store registered.")
        deliver_id = store.accumulate(
            graph_instance_id=self._graph_instance_id,
            node_id=target_node_id,
            source_node_id=source_node_id,
            source_invocation_id=source_invocation_id,
            content=content,
        )
        self.emit_output(
            GraphOutputKind.DELIVER_DISPATCHED,
            node_id=source_node_id,
            node_name=source_node_name,
            target_node_id=target_node_id,
        )
        return deliver_id

    # ── Consumption methods ─────────────────────────────────────────────

    def collect_consumable_delivers(
        self, node_id: str, invocation_id: int
    ) -> list[DeliverRecord]:
        """Collect delivers consumable by this invocation.

        Delegates to ``deliver_store.query_consumable``. Returns PENDING +
        CONSUMED_PENDING (SQLite) or just PENDING (InMemory).

        Args:
            node_id: The consuming node ID.
            invocation_id: The consuming invocation's ID.

        Returns:
            Consumable ``DeliverRecord`` list, or empty list if no store.
        """
        store = self._deliver_stores.get(node_id)
        if store is None:
            return []
        return store.query_consumable(self._graph_instance_id, node_id)

    def mark_delivers_consumed(
        self, node_id: str, deliver_ids: list[int], invocation_id: int
    ) -> None:
        """Mark delivers as consumed by an invocation.

        Delegates to ``deliver_store.mark_consumed``.
        """
        store = self._deliver_stores.get(node_id)
        if store is not None:
            store.mark_consumed(deliver_ids, invocation_id)

    def promote_delivers(self, node_id: str, invocation_id: int) -> None:
        """Promote consumed delivers on invocation completion.

        Promotes ALL CONSUMED_PENDING delivers for this node, not just
        those matching ``invocation_id``. This fixes the resume scenario
        where a prior suspended invocation's CONSUMED_PENDING delivers
        (consumed_by=that invocation) are not promoted when the resumed
        invocation completes.

        Args:
            node_id: The node ID whose delivers to promote.
            invocation_id: The completing invocation's ID.
        """
        store = self._deliver_stores.get(node_id)
        if store is None:
            return
        consumable = store.query_consumable(self._graph_instance_id, node_id)
        consumed_pending_invocation_ids = {
            r.consumed_by_invocation_id
            for r in consumable
            if r.status == DeliverConsumptionStatus.CONSUMED_PENDING
            and r.consumed_by_invocation_id is not None
        }
        for inv_id in consumed_pending_invocation_ids:
            store.promote_consumed(inv_id)
        store.promote_consumed(invocation_id)

    # ── Recovery + state query ──────────────────────────────────────────────

    def rebuild_main_state(self) -> dict[str, Any]:
        """Rebuild ``main_state`` from the single globally-newest full snapshot.

        Full snapshots are shared-state: the latest ``COMPLETED`` or
        ``suspended RUNNING`` record already contains all prior accumulated
        state (imperative mutations from every prior node). Return that
        snapshot's ``state_json`` directly — no per-node merge needed.

        Single query ``max(updated_at, invocation_id)`` across
        all nodes' ``{COMPLETED, suspended RUNNING}`` records.
        """
        store = self._node_state_store
        candidates: list[NodeInvocationRecord] = []
        for node_id in store.list_nodes():
            versions = store.query_versions(
                node_id, {InvocationStatus.COMPLETED, InvocationStatus.RUNNING}
            )
            candidates.extend(
                r
                for r in versions
                if r.status == InvocationStatus.COMPLETED
                or (r.status == InvocationStatus.RUNNING and r.suspended)
            )
        if not candidates:
            return {}
        latest = max(candidates, key=lambda r: (r.updated_at, r.invocation_id))
        return dict(latest.state_json)

    def get_graph_state(
        self, node_status_filter: set[InvocationStatus] | None = None
    ) -> GraphStateSnapshot:
        """Collect graph metadata + per-node version histories.

        Args:
            node_status_filter: Optional set of statuses to filter by. None
                returns all versions.

        Returns:
            ``GraphStateSnapshot`` with metadata and per-node version lists.
        """
        gid = self._graph_instance_id
        store = self._node_state_store
        metadata = self._instance_store.load(gid)
        if metadata is None:
            metadata = GraphMetadata(
                graph_instance_id=gid,
                spec_id=0,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.RUNNING,
            )
        nodes: dict[str, list[NodeInvocationRecord]] = {}
        for node_id in self._deliver_stores:
            nodes[node_id] = store.query_versions(node_id, node_status_filter)
        return GraphStateSnapshot(metadata=metadata, nodes=nodes)

    # ── Resource cleanup ──────────────────────────────────────────────────

    def close(self) -> None:
        """No-op — the coordinator owns no connections.

        Stores take a caller-owned ``sqlite3.Connection`` and never close
        it; the caller manages the connection lifetime. Retained as a
        safe-to-call lifecycle hook for
        ``GraphOrchestrator.unregister_instance``.
        """


def create_null_coordinator(graph_instance_id: int = 0) -> GraphPersistenceCoordinator:
    """Create a Null coordinator — no-op persistence, in-memory deliver queue.

    The Null strategy is the correct no-persistence implementation (rule 15
    compliant — not a backward-compat shim). Used by:

    - ``ReActAgent.actual_turn`` — per-turn coordinator (no GraphInstance
      persistence; AgentContext holds turn state orthogonally).
    - ``LLMNode`` — module-level governance helper context (``Node.run()``
      is never called on it).
    """
    return GraphPersistenceCoordinator(
        graph_instance_id=graph_instance_id,
        instance_store=NullGraphInstanceStore(),
        node_state_store=NullNodeStateStore(graph_instance_id),
        default_deliver_store_factory=NullDeliverStoreFactory(),
    )


class CoordinatorFactory(ABC):
    """Create a ``GraphPersistenceCoordinator`` for a graph instance.

    The factory receives the caller's exact ``instance_store`` instance
    (shared with the orchestrator and recovery service) and assembles the
    remaining stores — ``node_state_store`` and ``deliver_store_factory`` —
    internally. This is business-layer assembly: the framework does not
    prescribe how those stores are constructed (Null, InMemory, or SQLite
    with a shared connection).

    The framework default is ``NullCoordinatorFactory`` (no-op
    persistence). Business layers substitute a factory that wires stores
    to a shared ``sqlite3.Connection`` for crash recovery.
    """

    @abstractmethod
    def create(
        self,
        graph_instance_id: int,
        instance_store: GraphInstanceStore,
    ) -> GraphPersistenceCoordinator:
        """Create a coordinator bound to ``graph_instance_id``.

        Args:
            graph_instance_id: The persistence key for this run.
            instance_store: The caller-owned instance store. The factory
                must use this exact instance (shared with the orchestrator
                and recovery service), not a freshly-constructed one.

        Returns:
            A ``GraphPersistenceCoordinator`` with ``node_state_store``
            and ``deliver_store_factory`` assembled by the factory.
        """
        ...


class NullCoordinatorFactory(CoordinatorFactory):
    """Framework default factory — Null persistence strategy.

    Creates a coordinator with the passed ``instance_store`` plus
    ``NullNodeStateStore`` and ``NullDeliverStoreFactory``. The passed
    ``instance_store`` is used as-is (not replaced with a Null one), so
    the coordinator shares the caller's instance store while node state
    and delivers remain no-op.
    """

    def create(
        self,
        graph_instance_id: int,
        instance_store: GraphInstanceStore,
    ) -> GraphPersistenceCoordinator:
        return GraphPersistenceCoordinator(
            graph_instance_id=graph_instance_id,
            instance_store=instance_store,
            node_state_store=NullNodeStateStore(graph_instance_id),
            default_deliver_store_factory=NullDeliverStoreFactory(),
        )


__all__ = [
    "GraphPersistenceCoordinator",
    "CoordinatorFactory",
    "NullCoordinatorFactory",
    "create_null_coordinator",
]
