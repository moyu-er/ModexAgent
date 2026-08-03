# ruff: noqa: ANN401

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from ..constants import GraphInstanceStatus
from .node_state import NodeInvocationRecord


class GraphMetadata(BaseModel):
    """Graph instance metadata for scheduler bookkeeping and lifecycle state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph_instance_id: int
    spec_id: int
    parent_instance_id: int | None
    parent_node: str | None
    status: GraphInstanceStatus
    instance_seq: int
    iteration_count: int
    activated_sources: dict[str, list[str]]
    pending_dispatches: dict[str, dict[str, list[dict[str, Any] | None]]]


class InvocationContext(BaseModel):
    """Context returned when a node invocation begins."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: int
    node_name: str
    version: int
    parent_version: int | None


class RecoveryContext(BaseModel):
    """State used to recover a graph instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metadata: GraphMetadata
    node_states: dict[str, NodeInvocationRecord | None]
    rebuilt_main_state: dict[str, Any]


class GraphStateSnapshot(BaseModel):
    """Persisted graph metadata and node invocation histories."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metadata: GraphMetadata
    nodes: dict[str, list[NodeInvocationRecord]]


__all__ = ["GraphMetadata", "InvocationContext", "RecoveryContext", "GraphStateSnapshot"]
