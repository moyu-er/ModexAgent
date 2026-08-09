"""Frozen Pydantic request/response structs for the graph REST API.

All models are ``frozen=True, extra="forbid"`` per project type-safety rules.
Imported by :mod:`bot.webui.routes.graph_routes` for request parsing and
response serialization.
"""

from __future__ import annotations

from typing import Any

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
    """Per-node status in a graph instance response (L2: both name and id).

    ``result`` carries the completed node's output summary (§11.4). Populated
    only for completed nodes whose state checkpoint contains a ``result`` key
    (e.g. END nodes using ``DefaultGraphState``). ``None`` for non-completed
    nodes or nodes whose state has no result field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str
    node_id: str
    status: str
    result: GraphPayload | None = None
    session_id: str | None = None


class GraphInstanceResponse(BaseModel):
    """``GET /api/graphs/instances/{id}`` — instance state + node statuses.

    ``graph_instance_id`` is ``str`` to avoid JS precision loss (see
    :class:`GraphSpecSummary`). ``spec_id`` is ``str`` for the same reason;
    the frontend uses it to fetch the spec YAML for MiniTopology rendering
    (graph visualization redesign G06). ``created_at`` / ``updated_at``
    are epoch ms (ADR-0029), sourced from ``GraphMetadata``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec_id: str
    graph_instance_id: str
    status: str
    nodes: list[NodeStatusInfo]
    result: list[GraphPayload] | None = None
    created_at: int = 0
    updated_at: int = 0


class GraphRunRecordResponse(BaseModel):
    """``GET /api/graphs/specs/{spec_id}/runs`` — one run's I/O + status.

    Joins ``GraphIORecord`` (``user_input``, ``output``, ``created_at``)
    with ``GraphMetadata`` (``status``, ``updated_at``). ``record_id`` and
    ``graph_instance_id`` are ``str`` to avoid JS precision loss (see
    :class:`GraphSpecSummary`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_id: str
    graph_instance_id: str
    user_input: GraphPayload | None = None
    output: list[GraphPayload] | None = None
    status: str
    created_at: int = 0
    updated_at: int = 0


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


class NodeTopologyInfo(BaseModel):
    """Per-node topology info (§11.3) — name, type, config, trigger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    node_type: str
    config: dict[str, Any] = {}
    trigger: str | None = None


class EdgeTopologyInfo(BaseModel):
    """Per-edge topology info (§11.3) — source → target."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    target: str


class GraphTopologyResponse(BaseModel):
    """``GET /api/graphs/specs/{spec_id}/topology`` — compiled topology (§11.3).

    ``spec_id`` is ``str`` to avoid JS precision loss (see
    :class:`GraphSpecSummary`). ``entry_node`` is always ``"__start__"``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec_id: str
    name: str
    scheduler: str
    default_trigger: str
    nodes: list[NodeTopologyInfo]
    edges: list[EdgeTopologyInfo]
    entry_node: str


__all__ = [
    "EdgeTopologyInfo",
    "GraphDeliverRequest",
    "GraphEventItem",
    "GraphEventListResponse",
    "GraphInstanceResponse",
    "GraphRunRecordResponse",
    "GraphRunRequest",
    "GraphRunResponse",
    "GraphSpecListResponse",
    "GraphSpecResponse",
    "GraphSpecSummary",
    "GraphSpecUpdateRequest",
    "GraphTopologyResponse",
    "NodeStatusInfo",
    "NodeTopologyInfo",
]
