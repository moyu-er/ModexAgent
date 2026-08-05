"""SQLite persistence assembly for graph orchestration."""

from __future__ import annotations

import sqlite3

from modex_graph import (
    CoordinatorFactory,
    GraphInstanceStore,
    GraphPersistenceCoordinator,
    SqliteDeliverStoreFactory,
    SqliteNodeStateStore,
)


class SqliteCoordinatorFactory(CoordinatorFactory):
    """Assemble graph coordinators on one caller-owned SQLite connection."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def create(
        self,
        graph_instance_id: int,
        instance_store: GraphInstanceStore,
    ) -> GraphPersistenceCoordinator:
        return GraphPersistenceCoordinator(
            graph_instance_id=graph_instance_id,
            instance_store=instance_store,
            node_state_store=SqliteNodeStateStore(
                self._connection,
                graph_instance_id,
            ),
            default_deliver_store_factory=SqliteDeliverStoreFactory(self._connection),
        )


__all__ = ["SqliteCoordinatorFactory"]
