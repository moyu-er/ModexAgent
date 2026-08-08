"""Distributed persistence layer: stores, coordinator, and graph instance.

Sub-package grouping the persistence model:

1. **Graph instance metadata** — ``GraphInstanceStore`` (Null / InMemory /
   SQLite). Persists ``GraphMetadata`` (5 identity/status fields as
   individual columns). Scheduler bookkeeping is derived at recovery
   time, not persisted.
2. **Node invocation** — ``NodeStateStore`` (Null / InMemory / Sqlite) —
   lifecycle + version chain + CAS authority, scoped to one
   ``graph_instance_id``.
3. **Deliver** — per-node ``DeliverStore`` (Null / InMemory / Sqlite) with
   consumption state machine.

``GraphPersistenceCoordinator`` is the central orchestrator that unifies
deliver routing + recovery. Node lifecycle methods (begin / complete /
cancel / suspend / crash / finalize) live on ``NodeStateStore``, called
directly by ``Node.run()`` via ``ctx.node_state_store``. The coordinator
handles deliver routing, recovery, and state queries.

Modules:

- ``deliver_store`` — ``DeliverStore`` ABC + Null / InMemory / Sqlite impls +
  ``DeliverRecord`` value object.
- ``node_state_store`` — ``NodeStateStore`` ABC + Null / InMemory / Sqlite
  impls (lifecycle + CAS + version chain).
- ``graph_metadata`` — ``GraphMetadata`` / ``InvocationContext`` /
  ``NodeInvocationRecord`` / ``GraphStateSnapshot`` value objects.
- ``instance_store`` — ``GraphInstanceStore`` ABC + Null / InMemory / Sqlite.
- ``persistence_coordinator`` — ``GraphPersistenceCoordinator`` +
  ``create_null_coordinator`` factory.
- ``graph_instance`` — ``GraphInstance`` runtime instance.
"""

from __future__ import annotations

from .deliver_store import (
    DeliverRecord,
    DeliverStore,
    DeliverStoreFactory,
    InMemoryDeliverStore,
    InMemoryDeliverStoreFactory,
    NullDeliverStore,
    NullDeliverStoreFactory,
    SqliteDeliverStore,
    SqliteDeliverStoreFactory,
)
from .graph_instance import GraphInstance
from .graph_metadata import (
    GraphMetadata,
    GraphStateSnapshot,
    InvocationContext,
    NodeInvocationRecord,
)
from .instance_store import (
    GraphInstanceStore,
    InMemoryGraphInstanceStore,
    NullGraphInstanceStore,
    SqliteGraphInstanceStore,
)
from .node_state_store import (
    InMemoryNodeStateStore,
    NodeStateStore,
    NullNodeStateStore,
    SqliteNodeStateStore,
)
from .persistence_coordinator import (
    CoordinatorFactory,
    GraphPersistenceCoordinator,
    NullCoordinatorFactory,
    create_null_coordinator,
)

__all__ = [
    # Deliver/submit persistence
    "DeliverRecord",
    "DeliverStore",
    "DeliverStoreFactory",
    "InMemoryDeliverStore",
    "InMemoryDeliverStoreFactory",
    "NullDeliverStore",
    "NullDeliverStoreFactory",
    "SqliteDeliverStore",
    "SqliteDeliverStoreFactory",
    # Node state persistence (lifecycle + CAS + version chain)
    "NodeStateStore",
    "NullNodeStateStore",
    "InMemoryNodeStateStore",
    "SqliteNodeStateStore",
    # Graph metadata
    "GraphMetadata",
    "InvocationContext",
    "NodeInvocationRecord",
    "GraphStateSnapshot",
    # Graph instance store
    "GraphInstanceStore",
    "NullGraphInstanceStore",
    "InMemoryGraphInstanceStore",
    "SqliteGraphInstanceStore",
    # Persistence coordinator
    "GraphPersistenceCoordinator",
    "CoordinatorFactory",
    "NullCoordinatorFactory",
    "create_null_coordinator",
    # Runtime graph instance
    "GraphInstance",
]
