# ruff: noqa: ANN401

"""``GraphPersistenceCoordinator`` — distributed persistence core.

Orchestrates node lifecycle events + persistence routing. The coordinator is
the single seam through which the scheduler (or any caller) drives node
invocation persistence. It holds:

- ``graph_instance_id`` — the persistence key binding all stores to one run.
- ``graph_metadata_store`` — graph-instance-level metadata (status, seq, ...).
- ``node_states: dict[str, NodeState]`` — per-node invocation version chain.
- ``deliver_stores: dict[str, DeliverStore]`` — per-node deliver accumulation.

Lifecycle methods:

- ``begin_invocation`` — create PENDING → RUNNING invocation. parent_version
  is computed internally from ``load_latest_completed``. version = max(all
  existing versions) + 1. If a suspended=True RUNNING invocation exists, mark
  it SUPERSEDED. Internal try/except self-cleanup on failure. SUPERSEDED
  marking + new invocation creation — atomicity is achieved via recovery
  semantics (SUPERSEDED with no successor → re-dispatch), not strict DB
  transactions.
- ``complete_invocation`` — save COMPLETED + promote_delivers. save + promote
  atomicity is achieved via recovery auto-promote. promote ALL
  CONSUMED_PENDING for the node (not just current invocation_id).
- ``cancel_invocation`` / ``crash_invocation`` — save CANCELED / CRASHED.
- ``suspend_invocation`` — save RUNNING + state_json=snapshot + suspended=True.
  GraphInterrupt path (not crash/cancel).
- ``finalize_invocation`` — safety net: orphan PENDING (suspended=False) →
  CRASHED; suspended=True RUNNING untouched; SUPERSEDED untouched.

Consumption methods:

- ``collect_consumable_delivers`` — delegate to ``deliver_store.query_consumable``.
- ``mark_delivers_consumed`` — delegate to ``deliver_store.mark_consumed``.
- ``promote_delivers`` — promote ALL CONSUMED_PENDING for the node.

Recovery methods:

- ``load_for_recovery`` — load metadata + each node ``load_latest`` + rebuild
  main_state. auto-promote CONSUMED_PENDING delivers whose consuming
  invocation is COMPLETED (crash-between save-COMPLETED and promote). Returns
  ``RecoveryContext`` with ``rebuilt_main_state``.
- ``rebuild_main_state`` — sort COMPLETED records by invocation_id (global
  Snowflake time order), apply state_json; finally apply SUPERSEDED snapshots
  (last — they carry suspend-time imperative state like ``resume_target``).
- ``load_latest_invocation`` — load latest invocation (for resume check).
- ``get_graph_state`` — collect metadata + each node ``query_versions``.

Routing methods:

- ``route_deliver`` — target == END → skip (return None); else delegate to
  ``deliver_store.accumulate``. Raises ``RoutingError`` if no store registered.
- ``get_deliver_store`` — external query.
"""

from __future__ import annotations

from typing import Any

from .constants import (
    DeliverConsumptionStatus,
    GraphInstanceStatus,
    GraphNode,
    InvocationStatus,
)
from .deliver_store import DeliverRecord, DeliverStore, DeliverStoreFactory, NullDeliverStoreFactory
from .exceptions import RoutingError
from .graph_metadata import (
    GraphMetadata,
    GraphStateSnapshot,
    InvocationContext,
    RecoveryContext,
)
from .graph_metadata_store import GraphMetadataStore, NullGraphMetadataStore
from .id_generator import default_id_generator
from .node_state import (
    NodeInvocationRecord,
    NodeState,
    NodeStateFactory,
    NullNodeStateFactory,
)


class GraphPersistenceCoordinator:
    """Unified node lifecycle event + persistence routing coordinator.

    The scheduler is unaware of persistence stores; the coordinator holds
    graph metadata store + per-node NodeState / DeliverStore references and
    calls the appropriate methods at lifecycle event points.

    Three implementation strategies:

    - **Null** — ``NullGraphMetadataStore`` + ``NullNodeState`` + ``NullDeliverStore``.
      LinearScheduler default; graphs that don't need persistence.
    - **Memory** — ``MemoryGraphMetadataStore`` + ``SimpleNodeState`` + ``InMemoryDeliverStore``.
      Tests; single-process ephemeral graphs.
    - **SQLite** — ``SqliteGraphMetadataStore`` + ``SqliteNodeState`` + ``SqliteDeliverStore``.
      Production; graphs requiring crash recovery.
    """

    def __init__(
        self,
        graph_instance_id: int,
        graph_metadata_store: GraphMetadataStore,
        default_node_state_factory: NodeStateFactory,
        default_deliver_store_factory: DeliverStoreFactory,
    ) -> None:
        """Initialize the coordinator.

        Args:
            graph_instance_id: The persistence key binding all stores to one run.
            graph_metadata_store: Graph-instance-level metadata store.
            default_node_state_factory: Factory for creating NodeState when
                ``register_node`` is called without an explicit ``node_state``.
            default_deliver_store_factory: Factory for creating DeliverStore
                when ``register_node`` is called without an explicit
                ``deliver_store``. Required (not Optional) —
                ``NullDeliverStoreFactory`` is the no-persistence default.
        """
        self._graph_instance_id = graph_instance_id
        self._metadata_store = graph_metadata_store
        self._default_node_state_factory = default_node_state_factory
        self._default_deliver_store_factory = default_deliver_store_factory
        self._node_states: dict[str, NodeState] = {}
        self._deliver_stores: dict[str, DeliverStore] = {}

    # ── Registration + routing ───────────────────────────────────────

    def register_node(
        self,
        node_name: str,
        node_state: NodeState | None = None,
        deliver_store: DeliverStore | None = None,
    ) -> None:
        """Register a node's persistence strategy.

        ``None`` for ``node_state`` / ``deliver_store`` means "use the default
        factory".         The factories themselves are always required and were
        injected at construction time.

        Args:
            node_name: The node to register.
            node_state: Optional explicit NodeState. None → default factory.
            deliver_store: Optional explicit DeliverStore. None → default factory.
        """
        state = node_state if node_state is not None else self._default_node_state_factory.create()
        self._node_states[node_name] = state
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
            invocation_id: The consuming invocation's ID (unused by the
                current ``query_consumable`` signature — kept for API
                symmetry with the design spec).

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

        Args:
            node_name: The consuming node.
            deliver_ids: The deliver_ids to mark.
            invocation_id: The consuming invocation's ID.
        """
        store = self._deliver_stores.get(node_name)
        if store is not None:
            store.mark_consumed(deliver_ids, invocation_id)

    def promote_delivers(self, node_name: str, invocation_id: int) -> None:
        """Promote consumed delivers on invocation completion.

        Promotes ALL CONSUMED_PENDING delivers for this node, not just
        those matching ``invocation_id``. This fixes the resume scenario
        where a superseded v4's CONSUMED_PENDING delivers (consumed_by=v4)
        are not promoted when v5 completes.

        Implementation: ``query_consumable`` returns PENDING + CONSUMED_PENDING
        (SQLite) or just PENDING (InMemory). Filter for CONSUMED_PENDING,
        extract their ``consumed_by_invocation_id`` values, and call
        ``promote_consumed`` for each distinct invocation. Then also promote
        the current invocation's delivers (for InMemory
        two-state, this deletes CONSUMED records for this invocation).

        Args:
            node_name: The node whose delivers to promote.
            invocation_id: The completing invocation's ID.
        """
        store = self._deliver_stores.get(node_name)
        if store is None:
            return
        # Find ALL CONSUMED_PENDING delivers for this node (any invocation).
        consumable = store.query_consumable(self._graph_instance_id, node_name)
        consumed_pending_invocation_ids = {
            r.consumed_by_invocation_id
            for r in consumable
            if r.status == DeliverConsumptionStatus.CONSUMED_PENDING
            and r.consumed_by_invocation_id is not None
        }
        for inv_id in consumed_pending_invocation_ids:
            store.promote_consumed(inv_id)
        # Also promote the current invocation's delivers.
        # For InMemory (two-state): deletes CONSUMED records for this invocation.
        # For SQLite (three-state): transitions CONSUMED_PENDING → CONSUMED_COMPLETED.
        # Idempotent — may be redundant if already in the set above.
        store.promote_consumed(invocation_id)

    # ── Lifecycle methods ───────────────────────────────────────────────────

    def begin_invocation(self, node_name: str) -> InvocationContext:
        """Create a new invocation (PENDING → RUNNING).

        ``parent_version`` is computed internally from
        ``load_latest_completed`` — not a parameter.

        ``version = max(all existing versions) + 1`` (not
        ``load_latest_completed.version + 1`` — avoids UNIQUE conflicts when
        a CRASHED/RUNNING version exists above the latest COMPLETED).

        If a suspended=True RUNNING invocation exists, mark it
        SUPERSEDED before creating the new invocation.

        Safety net: orphan PENDING or non-suspended RUNNING (crash from a
        previous run) → mark CRASHED.

        Internal try/except — on failure, re-raise without cleanup.
        If the PENDING save succeeded but a subsequent step fails,
        ``Node.run()`` never receives the ``InvocationContext``, so its
        ``finally`` block cannot call ``finalize_invocation()``. The
        orphan PENDING record is instead cleaned up by recovery
        (safety net: orphan PENDING → CRASHED on next
        ``begin_invocation``, or by ``load_for_recovery``).

        SUPERSEDED marking + new invocation creation — atomicity is
        achieved via recovery semantics (SUPERSEDED with no
        successor → re-dispatch), not strict DB transactions. A crash
        between the SUPERSEDED mark and the PENDING creation leaves a
        SUPERSEDED record with no successor, which recovery handles by
        re-dispatching the node.

        Args:
            node_name: The node beginning an invocation.

        Returns:
            ``InvocationContext`` with the new invocation_id, version,
            and parent_version.

        Raises:
            RoutingError: If ``node_name`` is not registered.
        """
        node_state = self._node_states.get(node_name)
        if node_state is None:
            raise RoutingError(f"Node {node_name!r} not registered with coordinator.")
        gid = self._graph_instance_id
        try:
            latest = node_state.load_latest(gid, node_name)

            # Suspended=True RUNNING → SUPERSEDED (terminal, immutable).
            if (
                latest is not None
                and latest.status == InvocationStatus.RUNNING
                and latest.suspended
            ):
                node_state.save_invocation(
                    gid,
                    node_name,
                    latest.invocation_id,
                    latest.version,
                    latest.parent_version,
                    InvocationStatus.SUPERSEDED,
                    latest.state_json,
                    suspended=True,
                )

            # Safety net: orphan RUNNING (suspended=False) or PENDING → CRASHED.
            if (
                latest is not None
                and latest.status == InvocationStatus.RUNNING
                and not latest.suspended
            ):
                node_state.save_invocation(
                    gid,
                    node_name,
                    latest.invocation_id,
                    latest.version,
                    latest.parent_version,
                    InvocationStatus.CRASHED,
                    latest.state_json,
                )
            if latest is not None and latest.status == InvocationStatus.PENDING:
                node_state.save_invocation(
                    gid,
                    node_name,
                    latest.invocation_id,
                    latest.version,
                    latest.parent_version,
                    InvocationStatus.CRASHED,
                    latest.state_json,
                )

            # version = max(all existing versions) + 1.
            all_versions = node_state.query_versions(gid, node_name)
            version = max((r.version for r in all_versions), default=-1) + 1

            # parent_version from load_latest_completed.
            latest_completed = node_state.load_latest_completed(gid, node_name)
            parent_version = latest_completed.version if latest_completed is not None else None

            # PENDING → RUNNING. PENDING intermediate ensures a
            # crash between saves leaves a recoverable record (safety net).
            invocation_id = default_id_generator().generate()
            node_state.save_invocation(
                gid,
                node_name,
                invocation_id,
                version,
                parent_version,
                InvocationStatus.PENDING,
                {},
            )
            # PENDING → RUNNING; UPSERT replaces the PENDING record.
            node_state.save_invocation(
                gid,
                node_name,
                invocation_id,
                version,
                parent_version,
                InvocationStatus.RUNNING,
                {},
            )

            return InvocationContext(
                invocation_id=invocation_id,
                node_name=node_name,
                version=version,
                parent_version=parent_version,
            )
        except Exception:
            # Self-cleanup. The PENDING record (if created) is left for
            # finalize_invocation to mark as CRASHED. We cannot delete from
            # upsert-per-version stores; CRASHED marking is the cleanup. Re-raise.
            raise

    def complete_invocation(self, invocation: InvocationContext, state: dict[str, Any]) -> None:
        """Mark an invocation COMPLETED and promote consumed delivers.

        save COMPLETED + promote_delivers — atomicity is achieved via
        recovery auto-promote (scan for COMPLETED invocations
        whose delivers are still CONSUMED_PENDING → auto-promote). A crash
        between save COMPLETED and promote leaves CONSUMED_PENDING delivers
        that ``load_for_recovery`` auto-promotes.

        ``promote_delivers`` upgrades ALL CONSUMED_PENDING for the
        node (not just the current invocation_id) — fixes the resume case where
        a superseded v4's delivers aren't promoted by v5's completion.

        Args:
            invocation: The invocation context from ``begin_invocation``.
            state: The ``NodeResult.state_update`` to persist as ``state_json``.

        Raises:
            RoutingError: If the invocation's node is not registered.
        """
        node_state = self._node_states.get(invocation.node_name)
        if node_state is None:
            raise RoutingError(f"Node {invocation.node_name!r} not registered with coordinator.")
        # Save COMPLETED.
        node_state.save_invocation(
            self._graph_instance_id,
            invocation.node_name,
            invocation.invocation_id,
            invocation.version,
            invocation.parent_version,
            InvocationStatus.COMPLETED,
            state,
        )
        # Promote ALL CONSUMED_PENDING for this node.
        self.promote_delivers(invocation.node_name, invocation.invocation_id)

    def cancel_invocation(self, invocation: InvocationContext) -> None:
        """Mark an invocation CANCELED.

        CANCELED is a terminal state. Recovery skips CANCELED invocations
        (deliberate cancel — no auto re-dispatch; requires explicit resume).

        Args:
            invocation: The invocation context from ``begin_invocation``.

        Raises:
            RoutingError: If the invocation's node is not registered.
        """
        node_state = self._node_states.get(invocation.node_name)
        if node_state is None:
            raise RoutingError(f"Node {invocation.node_name!r} not registered with coordinator.")
        node_state.save_invocation(
            self._graph_instance_id,
            invocation.node_name,
            invocation.invocation_id,
            invocation.version,
            invocation.parent_version,
            InvocationStatus.CANCELED,
            {},
        )

    def suspend_invocation(
        self, invocation: InvocationContext, state_snapshot: dict[str, Any]
    ) -> None:
        """Suspend an invocation (GraphInterrupt path).

        Status stays RUNNING (not terminal — "not completed, awaiting
        resume"), but ``suspended=True`` marks it distinctly from an
        orphan/crash RUNNING. The ``state_snapshot`` (from
        ``ctx.state.checkpoint()``) is persisted as ``state_json`` — it
        carries imperative mutations like ``resume_target`` that the
        resumed node needs.

        On resume, ``begin_invocation`` marks this suspended RUNNING as
        SUPERSEDED, and ``rebuild_main_state`` applies the snapshot last
        (SUPERSEDED snapshots applied after all COMPLETED state_updates).

        Args:
            invocation: The invocation context from ``begin_invocation``.
            state_snapshot: The state checkpoint (from ``ctx.state.checkpoint()``).

        Raises:
            RoutingError: If the invocation's node is not registered.
        """
        node_state = self._node_states.get(invocation.node_name)
        if node_state is None:
            raise RoutingError(f"Node {invocation.node_name!r} not registered with coordinator.")
        node_state.save_invocation(
            self._graph_instance_id,
            invocation.node_name,
            invocation.invocation_id,
            invocation.version,
            invocation.parent_version,
            InvocationStatus.RUNNING,
            state_snapshot,
            suspended=True,
        )

    def crash_invocation(self, invocation: InvocationContext) -> None:
        """Mark an invocation CRASHED.

        CRASHED is a terminal state. Recovery re-dispatches CRASHED nodes
        (CRASHED + orphan PENDING/RUNNING → re-dispatch).

        Args:
            invocation: The invocation context from ``begin_invocation``.

        Raises:
            RoutingError: If the invocation's node is not registered.
        """
        node_state = self._node_states.get(invocation.node_name)
        if node_state is None:
            raise RoutingError(f"Node {invocation.node_name!r} not registered with coordinator.")
        node_state.save_invocation(
            self._graph_instance_id,
            invocation.node_name,
            invocation.invocation_id,
            invocation.version,
            invocation.parent_version,
            InvocationStatus.CRASHED,
            {},
        )

    def finalize_invocation(self, invocation: InvocationContext) -> None:
        """Safety net: ensure persistence state is consistent.

        Called in the ``finally`` block of ``Node.run()``. Rules:

        - Suspended=True RUNNING → untouched (HITL suspend, awaiting resume).
        - SUPERSEDED → untouched (already terminal, replaced by successor).
        - Orphan RUNNING (suspended=False, never reached terminal state) → CRASHED.
        - Orphan PENDING (crash between PENDING and RUNNING saves) → CRASHED.
        - COMPLETED / CANCELED / CRASHED → untouched (already terminal).

        Args:
            invocation: The invocation context from ``begin_invocation``.
        """
        node_state = self._node_states.get(invocation.node_name)
        if node_state is None:
            return
        gid = self._graph_instance_id
        latest = node_state.load_invocation(gid, invocation.node_name, invocation.invocation_id)
        if latest is None:
            return

        # Suspended RUNNING — don't touch.
        if latest.status == InvocationStatus.RUNNING and latest.suspended:
            return
        # SUPERSEDED — already terminal, don't touch.
        if latest.status == InvocationStatus.SUPERSEDED:
            return
        # Orphan RUNNING (suspended=False) or PENDING → CRASHED.
        if latest.status in (InvocationStatus.RUNNING, InvocationStatus.PENDING):
            node_state.save_invocation(
                gid,
                invocation.node_name,
                invocation.invocation_id,
                invocation.version,
                invocation.parent_version,
                InvocationStatus.CRASHED,
                {},
            )

    # ── Recovery + state query ──────────────────────────────────────────────

    def load_latest_invocation(self, node_name: str) -> NodeInvocationRecord | None:
        """Load a node's latest invocation (for resume check).

        Used by ``Node.run()`` integrate step to check if the previous
        invocation was SUPERSEDED with a state_snapshot (resume from
        suspend → skip re-consume).

        Args:
            node_name: The node to query.

        Returns:
            The latest ``NodeInvocationRecord``, or ``None`` if no
            invocations exist or the node is not registered.
        """
        node_state = self._node_states.get(node_name)
        if node_state is None:
            return None
        return node_state.load_latest(self._graph_instance_id, node_name)

    def rebuild_main_state(self) -> dict[str, Any]:
        """Rebuild ``main_state`` from COMPLETED + SUPERSEDED records.

        Sort COMPLETED records by ``invocation_id`` (global Snowflake
        time order — not per-node version). Apply ``state_json``
        (= ``NodeResult.state_update``) sequentially.

        Then sort SUPERSEDED records by ``invocation_id`` and apply their
        ``state_json`` (= suspend-time state snapshot) last. This ensures
        imperative mutations like ``resume_target`` (set during execute,
        captured in the suspend snapshot) are visible to the resumed node.

        Returns:
            The rebuilt ``main_state`` dict.
        """
        gid = self._graph_instance_id
        all_completed: list[NodeInvocationRecord] = []
        all_superseded: list[NodeInvocationRecord] = []
        for node_name, node_state in self._node_states.items():
            completed = node_state.query_versions(gid, node_name, {InvocationStatus.COMPLETED})
            all_completed.extend(completed)
            superseded = node_state.query_versions(gid, node_name, {InvocationStatus.SUPERSEDED})
            all_superseded.extend(superseded)

        # Global sort by invocation_id (Snowflake time order).
        all_completed.sort(key=lambda r: r.invocation_id)
        all_superseded.sort(key=lambda r: r.invocation_id)

        rebuilt: dict[str, Any] = {}
        for record in all_completed:
            rebuilt.update(record.state_json)
        for record in all_superseded:
            rebuilt.update(record.state_json)
        return rebuilt

    def load_for_recovery(self) -> RecoveryContext:
        """Load recovery context: metadata + node states + rebuilt main_state.

        Returns ``RecoveryContext`` with ``rebuilt_main_state`` — the
        scheduler uses it directly without an extra rebuild call.

        Auto-promote CONSUMED_PENDING delivers whose consuming invocation
        is COMPLETED (crash between save COMPLETED and promote_delivers).
        Scans all nodes' deliver stores for CONSUMED_PENDING records whose
        ``consumed_by_invocation_id`` corresponds to a COMPLETED invocation,
        and promotes them.

        Returns:
            ``RecoveryContext`` with metadata, per-node latest invocation
            records, and rebuilt main_state.
        """
        gid = self._graph_instance_id
        metadata = self._metadata_store.load(gid)
        if metadata is None:
            # Fresh graph — no metadata saved yet. Return a minimal context
            # with default metadata; the caller initializes fresh state.
            # node_states still lists registered nodes (value=None — no
            # invocations to recover).
            metadata = GraphMetadata(
                graph_instance_id=gid,
                spec_id=0,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.RUNNING,
                instance_seq=0,
                iteration_count=0,
                activated_sources={},
                pending_dispatches={},
            )
            node_states: dict[str, NodeInvocationRecord | None] = dict.fromkeys(
                self._node_states, None
            )
            return RecoveryContext(
                metadata=metadata,
                node_states=node_states,
                rebuilt_main_state={},
            )

        node_states = {}
        for node_name, node_state in self._node_states.items():
            node_states[node_name] = node_state.load_latest(gid, node_name)

        # Auto-promote CONSUMED_PENDING delivers whose consuming
        # invocation is COMPLETED. This repairs the crash-between
        # save-COMPLETED-and-promote inconsistency.
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
        invocation (checked via ``load_invocation``), promote them.

        This repairs the crash-between save-COMPLETED and promote_delivers
        inconsistency.
        """
        gid = self._graph_instance_id
        for node_name, node_state in self._node_states.items():
            store = self._deliver_stores.get(node_name)
            if store is None:
                continue
            consumable = store.query_consumable(gid, node_name)
            for record in consumable:
                if (
                    record.status != DeliverConsumptionStatus.CONSUMED_PENDING
                    or record.consumed_by_invocation_id is None
                ):
                    continue
                # Check if the consuming invocation is COMPLETED.
                inv = node_state.load_invocation(gid, node_name, record.consumed_by_invocation_id)
                if inv is not None and inv.status == InvocationStatus.COMPLETED:
                    store.promote_consumed(record.consumed_by_invocation_id)

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
        metadata = self._metadata_store.load(gid)
        if metadata is None:
            metadata = GraphMetadata(
                graph_instance_id=gid,
                spec_id=0,
                parent_instance_id=None,
                parent_node=None,
                status=GraphInstanceStatus.RUNNING,
                instance_seq=0,
                iteration_count=0,
                activated_sources={},
                pending_dispatches={},
            )
        nodes: dict[str, list[NodeInvocationRecord]] = {}
        for node_name, node_state in self._node_states.items():
            nodes[node_name] = node_state.query_versions(gid, node_name, node_status_filter)
        return GraphStateSnapshot(metadata=metadata, nodes=nodes)

    def update_graph_status(self, status: GraphInstanceStatus) -> None:
        """Update the graph instance status in the metadata store."""
        self._metadata_store.update_status(self._graph_instance_id, status)

    # ── Resource cleanup ──────────────────────────────────────────────────

    def close(self) -> None:
        """Close resources (SQLite connections, etc.).

        Called by ``GraphOrchestrator.unregister_instance``. Closes
        deliver stores and the metadata store if they have a ``close``
        method. ``NodeState`` stores don't have ``close`` (they share the
        connection with ``DeliverStore`` for SQLite — closing the
        ``DeliverStore`` closes the shared connection).

        Safe to call multiple times.
        """
        for store in self._deliver_stores.values():
            close = getattr(store, "close", None)
            if callable(close):
                close()
        close_meta = getattr(self._metadata_store, "close", None)
        if callable(close_meta):
            close_meta()


def create_null_coordinator(graph_instance_id: int = 0) -> GraphPersistenceCoordinator:
    """Create a Null coordinator — no-op persistence, in-memory deliver queue.

    The Null strategy is the correct no-persistence implementation (rule 15
    compliant — not a backward-compat shim). Used by:

    - ``ReActAgent.actual_turn`` — per-turn coordinator (no GraphInstance
      persistence; AgentContext holds turn state orthogonally).
    - ``LLMNode`` — module-level governance helper context (``Node.run()``
      is never called on it).
    - ``GraphOrchestrator._execute`` — per-run coordinator when persistence
      is not configured.

    Behavior:

    - ``begin_invocation`` — creates an ``InvocationContext`` (in-memory;
      provides ``invocation_id`` + ``version``). The ``NullNodeState``
      discards the saved record immediately.
    - ``complete`` / ``cancel`` / ``crash`` / ``suspend_invocation`` —
      no-op (``NullNodeState`` discards; ``NullGraphMetadataStore`` ignores).
    - ``collect_consumable_delivers`` — ``NullDeliverStore`` in-memory queue.
    - ``route_deliver`` — accumulate to the in-memory ``NullDeliverStore``.
    - ``load_for_recovery`` — returns a fresh ``RecoveryContext`` (empty
      ``rebuilt_main_state`` — no prior state to recover).

    Args:
        graph_instance_id: The persistence key. Defaults to ``0`` for
            per-turn / ephemeral use. Pass a real instance ID when the
            coordinator should be scoped to one graph run (e.g.
            ``GraphOrchestrator``).

    Returns:
        A ``GraphPersistenceCoordinator`` wired with Null stores.
    """
    return GraphPersistenceCoordinator(
        graph_instance_id=graph_instance_id,
        graph_metadata_store=NullGraphMetadataStore(),
        default_node_state_factory=NullNodeStateFactory(),
        default_deliver_store_factory=NullDeliverStoreFactory(),
    )


__all__ = ["GraphPersistenceCoordinator", "create_null_coordinator"]
