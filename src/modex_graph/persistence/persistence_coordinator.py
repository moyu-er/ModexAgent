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
- **Recovery** — ``load_for_recovery``, ``rebuild_main_state``,
  ``get_graph_state``. These query the store internally.
- **Registration** — ``register_node`` registers deliver stores.

Consumption methods:

- ``collect_consumable_delivers`` — delegate to ``deliver_store.query_consumable``.
- ``mark_delivers_consumed`` — delegate to ``deliver_store.mark_consumed``.
- ``promote_delivers`` — promote ALL CONSUMED_PENDING for the node.

Recovery methods:

- ``load_for_recovery`` — load metadata + each node's latest record from the
  store + rebuild main_state. Auto-promote CONSUMED_PENDING delivers whose
  consuming invocation is COMPLETED.
- ``rebuild_main_state`` — for each node, pick the single newest record from
  the union of COMPLETED and suspended RUNNING, ordered by
  ``updated_at DESC, invocation_id DESC``. Merge per-node winners in global
  ``invocation_id`` order.
- ``get_graph_state`` — collect metadata + each node's version history.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..constants import (
    DeliverConsumptionStatus,
    GraphInstanceStatus,
    GraphNode,
    InvocationStatus,
)
from ..exceptions import RoutingError
from .deliver_store import DeliverRecord, DeliverStore, DeliverStoreFactory, NullDeliverStoreFactory
from .graph_metadata import (
    GraphMetadata,
    GraphStateSnapshot,
    NodeInvocationRecord,
    RecoveryContext,
)
from .instance_store import GraphInstanceStore, NullGraphInstanceStore
from .node_state_store import NodeStateStore, NullNodeStateStore


class GraphPersistenceCoordinator:
    """Persistence routing + recovery coordinator.

    The scheduler is unaware of persistence stores; the coordinator holds
    the instance store + node state store + per-node DeliverStore references
    and routes deliver/consumption/recovery calls.

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

    @property
    def node_state_store(self) -> NodeStateStore:
        """The node state store (lifecycle + version chain + CAS authority)."""
        return self._node_state_store

    # ── Registration + routing ───────────────────────────────────────

    def register_node(
        self,
        node_name: str,
        deliver_store: DeliverStore | None = None,
    ) -> None:
        """Register a node's deliver store.

        ``None`` for ``deliver_store`` means "use the default factory".

        Args:
            node_name: The node to register.
            deliver_store: Optional explicit DeliverStore. None → default factory.
        """
        ds = (
            deliver_store
            if deliver_store is not None
            else self._default_deliver_store_factory.create()
        )
        self._deliver_stores[node_name] = ds

    def get_deliver_store(self, node_name: str) -> DeliverStore | None:
        """Get a node's deliver_store (for external deliver routing queries)."""
        return self._deliver_stores.get(node_name)

    def route_deliver(
        self,
        target_node: str,
        content: Any,
        source_node: str,
        source_invocation_id: int,
    ) -> int | None:
        """Route a deliver to the target node's deliver_store.

        If ``target_node == GraphNode.END``, skip (END has no deliver_store),
        return None.

        Args:
            target_node: The node receiving the deliver.
            content: The delivered content (JSON-serializable).
            source_node: The delivering node.
            source_invocation_id: The deliverer's invocation_id.

        Returns:
            The ``deliver_id`` (Snowflake), or ``None`` if target is END.

        Raises:
            RoutingError: If ``target_node`` is not END and has no
                deliver_store registered.
        """
        if target_node == GraphNode.END:
            return None
        store = self._deliver_stores.get(target_node)
        if store is None:
            raise RoutingError(f"Node {target_node!r} has no deliver_store registered.")
        return store.accumulate(
            graph_instance_id=self._graph_instance_id,
            target_node=target_node,
            source_node=source_node,
            source_invocation_id=source_invocation_id,
            content=content,
        )

    # ── Consumption methods ─────────────────────────────────────────────

    def collect_consumable_delivers(
        self, node_name: str, invocation_id: int
    ) -> list[DeliverRecord]:
        """Collect delivers consumable by this invocation.

        Delegates to ``deliver_store.query_consumable``. Returns PENDING +
        CONSUMED_PENDING (SQLite) or just PENDING (InMemory).

        Args:
            node_name: The consuming node.
            invocation_id: The consuming invocation's ID.

        Returns:
            Consumable ``DeliverRecord`` list, or empty list if no store.
        """
        store = self._deliver_stores.get(node_name)
        if store is None:
            return []
        return store.query_consumable(self._graph_instance_id, node_name)

    def mark_delivers_consumed(
        self, node_name: str, deliver_ids: list[int], invocation_id: int
    ) -> None:
        """Mark delivers as consumed by an invocation.

        Delegates to ``deliver_store.mark_consumed``.
        """
        store = self._deliver_stores.get(node_name)
        if store is not None:
            store.mark_consumed(deliver_ids, invocation_id)

    def promote_delivers(self, node_name: str, invocation_id: int) -> None:
        """Promote consumed delivers on invocation completion.

        Promotes ALL CONSUMED_PENDING delivers for this node, not just
        those matching ``invocation_id``. This fixes the resume scenario
        where a prior suspended invocation's CONSUMED_PENDING delivers
        (consumed_by=that invocation) are not promoted when the resumed
        invocation completes.

        Args:
            node_name: The node whose delivers to promote.
            invocation_id: The completing invocation's ID.
        """
        store = self._deliver_stores.get(node_name)
        if store is None:
            return
        consumable = store.query_consumable(self._graph_instance_id, node_name)
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

        Ticket 26: single query ``max(updated_at, invocation_id)`` across
        all nodes' ``{COMPLETED, suspended RUNNING}`` records.
        """
        store = self._node_state_store
        candidates: list[NodeInvocationRecord] = []
        for node_name in store.list_nodes():
            versions = store.query_versions(
                node_name, {InvocationStatus.COMPLETED, InvocationStatus.RUNNING}
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

    def load_for_recovery(self) -> RecoveryContext:
        """Load recovery context: metadata + node states + rebuilt main_state.

        Auto-promote CONSUMED_PENDING delivers whose consuming invocation
        is COMPLETED (crash between save COMPLETED and promote_delivers).

        Returns:
            ``RecoveryContext`` with metadata, per-node latest invocation
            records, and rebuilt main_state.
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
            node_states: dict[str, NodeInvocationRecord | None] = dict.fromkeys(
                self._deliver_stores
            )
            return RecoveryContext(
                metadata=metadata,
                node_states=node_states,
                rebuilt_main_state={},
            )

        node_states = {name: store.load_latest(name) for name in self._deliver_stores}

        self._auto_promote_completed_invocations(node_states)

        rebuilt_main_state = self.rebuild_main_state()

        return RecoveryContext(
            metadata=metadata,
            node_states=node_states,
            rebuilt_main_state=rebuilt_main_state,
        )

    def _auto_promote_completed_invocations(
        self, node_states: dict[str, NodeInvocationRecord | None]
    ) -> None:
        """Recovery: auto-promote CONSUMED_PENDING delivers for COMPLETED invocations.

        For each node, scan ``query_consumable`` for CONSUMED_PENDING records.
        If the ``consumed_by_invocation_id`` corresponds to a COMPLETED
        invocation (checked via ``load_by_invocation_id``), promote them.
        """
        gid = self._graph_instance_id
        store = self._node_state_store
        for node_name in self._deliver_stores:
            deliver_store = self._deliver_stores[node_name]
            consumable = deliver_store.query_consumable(gid, node_name)
            for record in consumable:
                if (
                    record.status != DeliverConsumptionStatus.CONSUMED_PENDING
                    or record.consumed_by_invocation_id is None
                ):
                    continue
                inv_record = store.load_by_invocation_id(
                    node_name, record.consumed_by_invocation_id
                )
                if (
                    inv_record is not None
                    and inv_record.status == InvocationStatus.COMPLETED
                ):
                    deliver_store.promote_consumed(record.consumed_by_invocation_id)

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
        for node_name in self._deliver_stores:
            nodes[node_name] = store.query_versions(node_name, node_status_filter)
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
