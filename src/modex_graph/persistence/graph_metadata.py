# ruff: noqa: ANN401

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from ..constants import GraphInstanceStatus, InvocationStatus


class GraphMetadata(BaseModel):
    """Graph instance metadata — basic identity and lifecycle status.

    Scheduler bookkeeping (instance_seq, iteration_count,
    activated_sources, pending_dispatches) is derived at recovery time
    from the node_states and deliver stores, not persisted here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph_instance_id: int
    spec_id: int
    parent_instance_id: int | None
    parent_node: str | None
    status: GraphInstanceStatus
    node_id_map: dict[str, str] = {}
    created_at: int = 0
    updated_at: int = 0


class InvocationContext(BaseModel):
    """Context returned when a node invocation begins."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: int
    node_id: str
    version: int
    parent_version: int | None


class NodeInvocationRecord(BaseModel):
    """Persistent record for one node invocation.

    One row per ``(graph_instance_id, node_id, version)`` in the
    ``node_states`` table. The record tracks the invocation lifecycle:
    ``status`` transitions from ``RUNNING`` (initial) to a terminal state
    (``COMPLETED`` / ``CANCELED`` / ``CRASHED``). ``suspended=True``
    marks a ``RUNNING`` invocation paused for HITL resume — its
    ``state_json`` carries the checkpoint snapshot.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: int
    graph_instance_id: int
    node_id: str
    version: int
    parent_version: int | None
    status: InvocationStatus
    state_json: dict[str, Any]
    suspended: bool = False
    created_at: int
    updated_at: int


class GraphStateSnapshot(BaseModel):
    """Persisted graph metadata and node invocation histories."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metadata: GraphMetadata
    nodes: dict[str, list[NodeInvocationRecord]]


__all__ = [
    "GraphMetadata",
    "InvocationContext",
    "NodeInvocationRecord",
    "GraphStateSnapshot",
]
