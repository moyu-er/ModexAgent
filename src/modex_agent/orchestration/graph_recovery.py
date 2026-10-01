"""Recovery selection; execution admission and lifecycle belong to the orchestrator.

Only explicit CRASHED instances are auto-recovered. RUNNING, PAUSING and
STOPPING are not proof of a dead executor: the owning process classifier must
mark them CRASHED first. PAUSED requires explicit resume, including after restart.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from modex_graph import GraphInstanceStatus, GraphInstanceStore, GraphInterrupt

if TYPE_CHECKING:
    from modex_agent.orchestration.graph_orchestrator import GraphOrchestrator

logger = logging.getLogger(__name__)


class GraphRecoveryService:
    """Select recovery candidates without changing status or assembling runtimes."""

    def __init__(
        self,
        instance_store: GraphInstanceStore,
        orchestrator: GraphOrchestrator,
    ) -> None:
        self._instance_store = instance_store
        self._orchestrator = orchestrator

    async def recover_crashed(self) -> list[int]:
        """Attempt explicitly classified crashes; isolate automatic recovery failures.

        Admission failures must not mutate an existing local or remote owner.
        Execution failures are finalized by that execution's orchestrator.
        """
        recovered: list[int] = []
        for metadata in self._instance_store.load_by_status(GraphInstanceStatus.CRASHED):
            gid = metadata.graph_instance_id
            try:
                await self._orchestrator._run_existing_instance(gid)
            except GraphInterrupt:
                raise
            except Exception:
                logger.exception("Recovery failed for graph instance %s", gid)
            else:
                recovered.append(gid)
        return recovered

    async def resume(self, graph_instance_id: int) -> None:
        """Wait for manual recovery; validation and execution failures propagate."""
        await self._orchestrator.resume(graph_instance_id)


__all__ = ["GraphRecoveryService"]
