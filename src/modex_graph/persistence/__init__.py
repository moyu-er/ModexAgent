"""Distributed persistence layer: stores, coordinator, and graph instance.

Sub-package grouping the three-layer persistence model (per
``docs/design/graph-orchestration/distributed-persistence.md``):

1. **Graph metadata** — ``GraphMetadataStore`` (Null / Memory / SQLite).
2. **Node invocation** — ``NodeState`` version-chain stores
   (Null / Simple / Sqlite) + ``NodeStateStore``.
3. **Deliver** — per-node ``DeliverStore`` (Null / InMemory / Sqlite) with
   consumption state machine.

``GraphPersistenceCoordinator`` is the central orchestrator that unifies node
lifecycle events (begin / complete / cancel / suspend / crash / finalize)
with persistence routing (deliver / collect / mark / promote). ``GraphInstance``
pairs a coordinator with a serializable ``GraphMetadata`` value object.

``DispatchStore`` is the shared base for dispatch event persistence, used by
both the scheduler and the coordinator. All stores share the same Null /
Memory / SQLite strategy triple, walking the same ABC.

Modules:

- ``dispatch_store`` — ``DispatchStore`` ABC + InMemory / Sqlite impls.
  Shared base (``now_ms`` helper) for all timestamped stores.
- ``deliver_store`` — ``DeliverStore`` ABC + Null / InMemory / Sqlite impls +
  ``DeliverRecord`` value object.
- ``node_state`` — ``NodeState`` ABC + Null / Simple / Sqlite impls +
  ``NodeInvocationRecord`` value object + factories.
- ``node_state_store`` — ``NodeStateStore`` ABC + InMemory / Sqlite impls.
- ``metadata`` — ``GraphMetadata`` / ``InvocationContext`` /
  ``RecoveryContext`` / ``GraphStateSnapshot`` value objects.
- ``metadata_store`` — ``GraphMetadataStore`` ABC + Null / Memory / Sqlite.
- ``instance_store`` — ``GraphInstanceStore`` ABC + InMemory / Sqlite.
- ``persistence_coordinator`` — ``GraphPersistenceCoordinator`` +
  ``create_null_coordinator`` factory.
- ``graph_instance`` — ``GraphInstance`` runtime instance.
"""

from __future__ import annotations

from .deliver_store import (
    DeliverRecord,
    DeliverStatus,
    DeliverStore,
    DeliverStoreFactory,
    InMemoryDeliverStore,
    InMemoryDeliverStoreFactory,
    NullDeliverStore,
    NullDeliverStoreFactory,
    SqliteDeliverStore,
    SqliteDeliverStoreFactory,
)
from .dispatch_store import (
    DispatchStore,
    InMemoryDispatchStore,
    SqliteDispatchStore,
)
from .graph_instance import GraphInstance
from .graph_metadata import (
    GraphMetadata,
    GraphStateSnapshot,
    InvocationContext,
    RecoveryContext,
)
from .graph_metadata_store import (
    GraphMetadataStore,
    MemoryGraphMetadataStore,
    NullGraphMetadataStore,
    SqliteGraphMetadataStore,
)
from .instance_store import (
    GraphInstanceStore,
    InMemoryGraphInstanceStore,
    SqliteGraphInstanceStore,
)
from .node_state import (
    NodeInvocationRecord,
    NodeState,
    NodeStateFactory,
    NullNodeState,
    NullNodeStateFactory,
    SimpleNodeState,
    SimpleNodeStateFactory,
    SqliteNodeState,
    SqliteNodeStateFactory,
)
from .node_state_store import (
    InMemoryNodeStateStore,
    NodeStateStore,
    SqliteNodeStateStore,
)
from .persistence_coordinator import (
    GraphPersistenceCoordinator,
    create_null_coordinator,
)

__all__ = [
    # Dispatch persistence
    "DispatchStore",
    "InMemoryDispatchStore",
    "SqliteDispatchStore",
    # Deliver/submit persistence
    "DeliverStatus",
    "DeliverRecord",
    "DeliverStore",
    "DeliverStoreFactory",
    "InMemoryDeliverStore",
    "InMemoryDeliverStoreFactory",
    "NullDeliverStore",
    "NullDeliverStoreFactory",
    "SqliteDeliverStore",
    "SqliteDeliverStoreFactory",
    # Node state persistence
    "NodeStateStore",
    "InMemoryNodeStateStore",
    "SqliteNodeStateStore",
    # Node state in-memory abstraction
    "NodeState",
    "SimpleNodeState",
    "NullNodeState",
    "SqliteNodeState",
    "NodeInvocationRecord",
    "NodeStateFactory",
    "NullNodeStateFactory",
    "SimpleNodeStateFactory",
    "SqliteNodeStateFactory",
    # Graph metadata
    "GraphMetadata",
    "InvocationContext",
    "RecoveryContext",
    "GraphStateSnapshot",
    # Graph metadata store
    "GraphMetadataStore",
    "NullGraphMetadataStore",
    "MemoryGraphMetadataStore",
    "SqliteGraphMetadataStore",
    # Graph instance store
    "GraphInstanceStore",
    "InMemoryGraphInstanceStore",
    "SqliteGraphInstanceStore",
    # Persistence coordinator
    "GraphPersistenceCoordinator",
    "create_null_coordinator",
    # Runtime graph instance
    "GraphInstance",
]
