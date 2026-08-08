"""Frozen Pydantic request/response structs for the graph REST API.

All models are ``frozen=True, extra="forbid"`` per project type-safety rules.
Imported by :mod:`bot.webui.routes.graph_routes` for request parsing and
response serialization.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from modex_graph import GraphPayload


class GraphRunRequest(BaseModel):
    """Body for ``POST /api/graphs/specs/{spec_id}/run``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_input: GraphPayload | None = None


class GraphDeliverRequest(BaseModel):
    """Body for ``POST /api/graphs/instances/{id}/deliver``.

    ``node_name`` is the human-readable node name (graph_instance_id
    travels in the URL path, not the body).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str
    content: GraphPayload


class GraphSpecUpdateRequest(BaseModel):
    """Body for ``PUT /api/graphs/specs/{spec_id}`` — raw YAML content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    yaml_content: str


class GraphSpecSummary(BaseModel):
    """REST-friendly spec metadata (no spec JSON).

    ``spec_id`` is ``str`` to avoid JavaScript ``Number`` precision loss:
    the backend integer exceeds ``2^53 - 1`` (MAX_SAFE_INTEGER).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec_id: str
    name: str
    version: str


class GraphSpecResponse(BaseModel):
    """Full spec response with reconstructed YAML content.

    ``spec_id`` is ``str`` (see :class:`GraphSpecSummary`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec_id: str
    name: str
    version: str
    yaml_content: str


class GraphSpecListResponse(BaseModel):
    """``GET /api/graphs/specs`` — list of spec summaries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    specs: list[GraphSpecSummary]


class NodeStatusInfo(BaseModel):
    """Per-node status in a graph instance response (L2: both name and id)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str
    node_id: str
    status: str


class GraphInstanceResponse(BaseModel):
    """``GET /api/graphs/instances/{id}`` — instance state + node statuses.

    ``graph_instance_id`` is ``str`` to avoid JS precision loss (see
    :class:`GraphSpecSummary`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph_instance_id: str
    status: str
    nodes: list[NodeStatusInfo]
    result: list[GraphPayload] | None = None


class GraphRunResponse(BaseModel):
    """``POST /api/graphs/specs/{spec_id}/run`` — instance ID + initial status.

    ``graph_instance_id`` is ``str`` (see :class:`GraphSpecSummary`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph_instance_id: str
    status: str


class GraphEventItem(BaseModel):
    """Single graph event item — typed view of ``GraphOutput.model_dump``.

    ``graph_instance_id`` is ``str`` (not ``int``) to avoid JS Number
    precision loss beyond ``2^53 - 1`` (ADR: spec_id/instance_id as string).
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    graph_instance_id: str
    kind: str


class GraphEventListResponse(BaseModel):
    """``GET /api/graphs/instances/{id}/events`` — event list for polling."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    events: list[GraphEventItem]


__all__ = [
    "GraphDeliverRequest",
    "GraphEventItem",
    "GraphEventListResponse",
    "GraphInstanceResponse",
    "GraphRunRequest",
    "GraphRunResponse",
    "GraphSpecListResponse",
    "GraphSpecResponse",
    "GraphSpecSummary",
    "GraphSpecUpdateRequest",
    "NodeStatusInfo",
]
