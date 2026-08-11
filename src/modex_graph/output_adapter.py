"""Graph execution output values and adapter contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class GraphOutputKind(StrEnum):
    """Graph execution event kinds — terminal outcomes + node-level events."""

    COMPLETED = "graph_completed"
    CRASHED = "graph_crashed"
    FAILED = "graph_failed"
    NODE_STARTED = "node_started"
    NODE_COMPLETED = "node_completed"
    NODE_CRASHED = "node_crashed"
    DELIVER_DISPATCHED = "deliver_dispatched"


class GraphOutput(BaseModel):
    """Output event emitted for a graph instance.

    Terminal events (``graph_completed`` / ``graph_crashed``) carry ``result``
    / ``error``. Node-level events (``node_started`` / ``node_completed`` /
    ``node_crashed``) carry ``node_id`` / ``node_name`` / ``invocation_id``.
    ``deliver_dispatched`` carries ``node_id`` (source) + ``target_node_id``.
    All events carry ``timestamp`` (epoch ms).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: GraphOutputKind
    graph_instance_id: int
    result: Any = None
    error: str | None = None
    node_id: str | None = Field(
        default=None,
        description="Node-level events: the node. deliver_dispatched: the source node.",
    )
    node_name: str | None = Field(default=None, description="Node-level events: node name.")
    invocation_id: int | None = Field(
        default=None, description="Node-level events: the invocation version."
    )
    target_node_id: str | None = Field(
        default=None, description="deliver_dispatched: the delivery target node."
    )
    timestamp: int | None = Field(default=None, description="Epoch milliseconds.")


class GraphOutputAdapter(ABC):
    """Receives graph execution outputs (terminal + node-level events)."""

    @abstractmethod
    async def emit(self, output: GraphOutput) -> None:
        """Emit a graph output event."""


__all__ = ["GraphOutput", "GraphOutputAdapter", "GraphOutputKind"]
