"""`GraphInstance` — runtime graph instance.

A `GraphInstance` is the persistence/recovery unit. It is created when a
`GraphSpec` is compiled and instantiated, and carries the
`graph_instance_id` (a Snowflake ID — the persistence unique key that
replaces the in-memory `run_id`) plus parent linkage for nested subgraphs.

The full chain (per `spec.py`):

    GraphSpec → GraphSpecCompiler → CompiledGraph → GraphInstance → GraphEngine

`GraphInstance` evolved from a frozen Pydantic
data record into a plain runtime class. It now holds:

- ``metadata: GraphMetadata`` — the serializable value object (frozen Pydantic).
  Stored by ``GraphInstanceStore``. Carries identity + status (5 fields).
  Scheduler bookkeeping is derived at recovery time from node_states and
  deliver stores.
- ``coordinator: GraphPersistenceCoordinator`` — the persistence coordinator.
  The coordinator lifecycle is bound to the ``GraphInstance``
  lifecycle: it persists node invocations + delivers and provides recovery
  state loading.

Callers that access ``graph_instance.graph_instance_id`` / ``.status`` /
``.spec_id`` / ``.parent_instance_id`` / ``.parent_node`` are unchanged —
these delegate to ``metadata`` via properties.

Methods:

- ``get_state()`` → delegates to ``coordinator.get_graph_state``.
- ``load_for_recovery()`` → delegates to ``coordinator.load_for_recovery``.

Status transitions are routed directly through the ``GraphInstanceStore``
(``store.update_status(gid, status)``) by the orchestrator / recovery /
control services that hold the store; ``GraphInstance`` no longer owns a
status-update method.

`graph_instance_id` is a Snowflake-format `int` (per `id_generator.py`),
matching the `BIGINT` column in the SQLite schema. This is the single persistence
key (rule 15: converge — replaces `run_id`).

Lifecycle status uses the `GraphInstanceStatus` StrEnum
(running/paused/stopped/crashed/completed/failed).
"""

from __future__ import annotations

from ..constants import GraphInstanceStatus
from .graph_metadata import GraphMetadata, GraphStateSnapshot, RecoveryContext
from .persistence_coordinator import GraphPersistenceCoordinator

__all__ = ["GraphInstance"]


class GraphInstance:
    """Runtime graph instance — holds coordinator + serializable metadata.

    A `GraphInstance` is created when a `GraphSpec` is compiled and
    instantiated. It pairs the serializable `GraphMetadata` (identity +
    status, 5 fields) with the `GraphPersistenceCoordinator` (node invocation
    persistence + deliver routing + recovery). Scheduler bookkeeping is
    derived at recovery time from node_states and deliver stores.

    The coordinator lifecycle is bound to the GraphInstance lifecycle. The
    metadata is the serializable value object stored by GraphInstanceStore.

    Properties delegate to ``metadata`` so callers that access
    ``graph_instance_id`` / ``status`` / ``spec_id`` / ``parent_instance_id``
    / ``.parent_node`` are unchanged from the frozen-Pydantic era.

    Attributes:
        metadata: The serializable `GraphMetadata` value object (frozen
            Pydantic). Stored by `GraphInstanceStore`.
        coordinator: The `GraphPersistenceCoordinator` for this instance.
            Provides node invocation persistence, deliver routing, and
            recovery state loading.
    """

    def __init__(self, metadata: GraphMetadata, coordinator: GraphPersistenceCoordinator) -> None:
        """Initialize the runtime graph instance.

        Args:
            metadata: The serializable `GraphMetadata` value object.
            coordinator: The persistence coordinator bound to this instance.
        """
        self.metadata = metadata
        self.coordinator = coordinator

    # ── Properties delegating to metadata ───────────────────────────────

    @property
    def graph_instance_id(self) -> int:
        """Snowflake ID — the persistence unique key (replaces run_id)."""
        return self.metadata.graph_instance_id

    @property
    def spec_id(self) -> int:
        """FK → graph_specs.spec_id."""
        return self.metadata.spec_id

    @property
    def parent_instance_id(self) -> int | None:
        """Parent graph instance ID for nested subgraphs; None for outer."""
        return self.metadata.parent_instance_id

    @property
    def parent_node(self) -> str | None:
        """Node name in the parent graph that created this instance."""
        return self.metadata.parent_node

    @property
    def status(self) -> GraphInstanceStatus:
        """Lifecycle status (GraphInstanceStatus StrEnum)."""
        return self.metadata.status

    # ── Methods ───────────────────────────────────────────────

    def get_state(self) -> GraphStateSnapshot:
        """Collect graph metadata + per-node version histories.

        Delegates to ``coordinator.get_graph_state``.
        """
        return self.coordinator.get_graph_state()

    def load_for_recovery(self) -> RecoveryContext:
        """Load recovery context: metadata + node states + rebuilt main_state.

        Delegates to ``coordinator.load_for_recovery``.
        """
        return self.coordinator.load_for_recovery()

