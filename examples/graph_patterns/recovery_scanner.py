"""Business-layer SQLite graph recovery scanner reference."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from modex_agent.orchestration import GraphOrchestrator, SqliteCoordinatorFactory
from modex_graph import (
    GraphInstanceStatus,
    GraphInstanceStore,
    GraphSpecStore,
    GraphState,
    NodeRegistry,
    SqliteGraphInstanceStore,
    SqliteGraphSpecStore,
)

logger = logging.getLogger(__name__)

_RECOVERABLE_STATUSES = (
    GraphInstanceStatus.CRASHED,
    GraphInstanceStatus.RUNNING,
)


def open_workspace_connection(workspace_root: Path) -> sqlite3.Connection:
    """Open the caller-owned graph database at `<workspace>/.modex/state.db`."""
    database_directory = workspace_root / ".modex"
    database_directory.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(database_directory / "state.db")


def assemble_sqlite_orchestrator(
    connection: sqlite3.Connection,
    *,
    node_registry: NodeRegistry,
    state_classes: Mapping[str, type[GraphState]],
) -> tuple[GraphOrchestrator, GraphSpecStore, GraphInstanceStore]:
    """Assemble graph stores and orchestration on one borrowed connection."""
    spec_store = SqliteGraphSpecStore(connection)
    instance_store = SqliteGraphInstanceStore(connection)
    orchestrator = GraphOrchestrator(
        node_registry=node_registry,
        state_classes=state_classes,
        spec_store=spec_store,
        instance_store=instance_store,
        coordinator_factory=SqliteCoordinatorFactory(connection),
    )
    return orchestrator, spec_store, instance_store


class RecoveryScanner:
    """Run startup and periodic recovery with a business-owned retry budget."""

    def __init__(
        self,
        orchestrator: GraphOrchestrator,
        instance_store: GraphInstanceStore,
        *,
        interval_seconds: float,
        max_recovery_attempts: int,
    ) -> None:
        self._orchestrator = orchestrator
        self._instance_store = instance_store
        self._interval_seconds = interval_seconds
        self._max_recovery_attempts = max_recovery_attempts
        self._attempts: dict[int, int] = {}

    async def recover_once(self) -> list[int]:
        """Recover one scan batch and fail instances that exhaust the budget.

        ``recover_crashed`` isolates per-instance failures: if one instance
        raises, the remaining are still attempted. Recovered instances reset
        their attempt count; candidates still in a recoverable status after
        the scan (attempted but failed) are penalized.
        """
        candidate_ids = self._load_candidate_ids()
        recovered_ids = await self._orchestrator.recover_crashed()
        self._record_attempts(candidate_ids, set(recovered_ids))
        return recovered_ids

    async def run(self) -> None:
        """Recover immediately at startup, then scan at the configured interval."""
        while True:
            try:
                recovered_ids = await self.recover_once()
                if recovered_ids:
                    logger.info("Recovered graph instances: %s", recovered_ids)
            except Exception:
                logger.exception("Graph recovery scan failed")
            await asyncio.sleep(self._interval_seconds)

    def _load_candidate_ids(self) -> list[int]:
        return [
            metadata.graph_instance_id
            for status in _RECOVERABLE_STATUSES
            for metadata in self._instance_store.load_by_status(status)
        ]

    def _record_attempts(self, candidate_ids: list[int], recovered_ids: set[int]) -> None:
        for graph_instance_id in candidate_ids:
            if graph_instance_id in recovered_ids:
                self._attempts.pop(graph_instance_id, None)
                continue

            metadata = self._instance_store.load(graph_instance_id)
            if metadata is None or metadata.status not in _RECOVERABLE_STATUSES:
                self._attempts.pop(graph_instance_id, None)
                continue

            attempt_count = self._attempts.get(graph_instance_id, 0) + 1
            if attempt_count >= self._max_recovery_attempts:
                self._instance_store.update_status(
                    graph_instance_id,
                    GraphInstanceStatus.FAILED,
                )
                self._attempts.pop(graph_instance_id, None)
                logger.error(
                    "Graph instance %s exhausted %s recovery attempts",
                    graph_instance_id,
                    self._max_recovery_attempts,
                )
            else:
                self._attempts[graph_instance_id] = attempt_count


__all__ = [
    "RecoveryScanner",
    "assemble_sqlite_orchestrator",
    "open_workspace_connection",
]
