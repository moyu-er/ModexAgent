"""WebUI graph output adapter -- in-memory event store for REST polling."""

from __future__ import annotations

from modex_graph import GraphOutput, GraphOutputAdapter


class WebUIGraphOutputAdapter(GraphOutputAdapter):
    """Collect terminal graph outputs into an in-memory store keyed by instance ID.

    The REST layer polls ``GET /api/graphs/instances/{id}/events`` and reads
    from the shared event store dict. No WebSocket broadcaster is involved
    -- the graph subsystem owns its own output seam, decoupled from the bot's
    streaming emitter.
    """

    def __init__(self, graph_event_store: dict[int, list[GraphOutput]]) -> None:
        self._store = graph_event_store

    async def emit(self, output: GraphOutput) -> None:
        self._store.setdefault(output.graph_instance_id, []).append(output)


__all__ = ["WebUIGraphOutputAdapter"]
