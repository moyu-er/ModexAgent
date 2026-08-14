"""WebUI graph output adapter -- event store + WebSocket subscriber fan-out."""

from __future__ import annotations

import asyncio
import logging

from modex_graph import GraphOutput, GraphOutputAdapter

logger = logging.getLogger(__name__)


class WebUIGraphOutputAdapter(GraphOutputAdapter):
    """Single emission seam for graph events, with two channels.

    Channel 1 (REST polling): append to ``graph_event_store``, read back by
    ``GET /api/graphs/instances/{id}/events``. Unchanged behavior.

    Channel 2 (WebSocket push): fan out to every subscriber queue registered
    under ``graph_event_subscribers[instance_id]`` (populated by the WS
    ``subscribe_graph`` action). Instances with no subscribers are a no-op.
    A failing queue is logged and skipped -- fan-out must never affect graph
    execution.
    """

    def __init__(
        self,
        graph_event_store: dict[int, list[GraphOutput]],
        graph_event_subscribers: dict[int, list[asyncio.Queue[GraphOutput]]] | None = None,
    ) -> None:
        self._store = graph_event_store
        self._subscribers = (
            graph_event_subscribers if graph_event_subscribers is not None else {}
        )

    async def emit(self, output: GraphOutput) -> None:
        self._store.setdefault(output.graph_instance_id, []).append(output)
        for queue in tuple(self._subscribers.get(output.graph_instance_id, ())):
            try:
                queue.put_nowait(output)
            except Exception:
                logger.warning(
                    "graph event fan-out failed for instance %s",
                    output.graph_instance_id,
                    exc_info=True,
                )


__all__ = ["WebUIGraphOutputAdapter"]
