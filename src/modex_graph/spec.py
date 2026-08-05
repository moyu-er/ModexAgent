# ruff: noqa: ANN401
"""`GraphSpec` + `NodeSpec` + `EdgeSpec` — declarative graph specification.

Per ticket 08: `GraphSpec` is the declarative, fully-serializable graph
description — the persistence unit. The full chain is:

    GraphSpec → GraphSpecCompiler → CompiledGraph → GraphInstance → GraphEngine

`GraphSpecCompiler` and `TopologyValidator` are P2 (out of scope here). This
module provides ONLY the data structures + basic structural validation.

`GraphSpec` is the alternative to the imperative `Graph` builder
(`src/modex_graph/graph.py`). Both produce graphs the engine can run; the
declarative form is what gets persisted to the `graph_specs` table
(ADR-0033 D9.1 — the deferred Preset graphs layer).

Edge model: `EdgeSpec` defines topology only (source → target), with no
`reason` field. The deliver/submit model replaces the transition model:
routing is explicit via `deliver(content, next_node, ctx)` at runtime.
Edges define which nodes can connect; conditional routing is done by the
node's `deliver()` call, not by edge matching.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .constants import GraphNode, NodeTrigger, SchedulerKind


class NodeSpec(BaseModel):
    """Declarative node specification — serializable, config-driven.

    A `NodeSpec` is a recipe for constructing a `Node` instance. The
    `node_type` string names a registered `NodeFactory` (e.g. `"function"`,
    `"agent"`, `"delay"`); the factory validates `config` against a Pydantic
    schema declared at registration time and constructs the `Node`.

    Fields:
    - `name`: node name in the graph (must be unique within a `GraphSpec`).
    - `node_type`: registered `NodeFactory` key. Looked up in `NodeRegistry`
      at compile time.
    - `config`: node-specific configuration. Validated by the factory's
      `config_schema()` at registration. Free-form `dict` here; the factory
      turns it into a typed model.
    - `trigger`: per-node trigger mode under `ParallelScheduler`. `None`
      means "use the graph's `default_trigger`".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    node_type: str
    config: dict[str, Any] = Field(default_factory=dict)
    trigger: NodeTrigger | None = None


class EdgeSpec(BaseModel):
    """Declarative edge specification — topology only.

    Per ticket 07: the deliver/submit model replaces the transition model.
    Edges define topology (which nodes can connect), NOT conditional routing.
    Conditional routing is done by the node's `deliver(content, target, ctx)`
    call at runtime — the engine routes the delivered payload to the named
    target.

    Fields:
    - `source`: source node name (or `GraphNode.START`).
    - `target`: target node name (or `GraphNode.END`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    target: str


class GraphSpec(BaseModel):
    """Declarative graph specification — fully serializable, the persistence unit.

    Per ticket 08: `GraphSpec` is what gets persisted to the `graph_specs`
    table. The full chain is
    `GraphSpec → GraphSpecCompiler → CompiledGraph → GraphInstance →
    GraphEngine`. `GraphSpecCompiler` and `TopologyValidator` are P2 (out
    of scope here).

    `state_class` names a `GraphState` subclass in the compiler's injected
    state-class mapping.

    Basic structural validation is done here (no duplicate node names, at
    least one node, at least one entry edge from `GraphNode.START`). Full
    topology validation (cycle detection, reachability, max_depth) is
    `TopologyValidator` in P2.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    nodes: list[NodeSpec] = Field(default_factory=list)
    edges: list[EdgeSpec] = Field(default_factory=list)
    state_class: str
    scheduler: SchedulerKind = SchedulerKind.LINEAR
    version: str = "1.0"
    metadata: dict[str, Any] = Field(default_factory=dict)
    max_iterations: int = 25
    default_trigger: NodeTrigger = NodeTrigger.ON_ALL_PREDS

    @model_validator(mode="after")
    def _validate_structure(self) -> GraphSpec:
        """Basic structural checks (full topology validation is P2).

        - At least one node.
        - No duplicate node names.
        - At least one entry edge from `GraphNode.START` to a real node.
        - `max_iterations` > 0.
        - Edge endpoints are either sentinels or reference declared nodes.
        """
        if not self.nodes:
            raise ValueError("GraphSpec must declare at least one node (got empty nodes list).")

        # No duplicate node names.
        names = [n.name for n in self.nodes]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(
                f"Duplicate node names in GraphSpec: {sorted(duplicates)}. "
                f"Node names must be unique."
            )

        if self.max_iterations <= 0:
            raise ValueError(f"GraphSpec.max_iterations must be > 0 (got {self.max_iterations}).")

        # At least one entry edge from GraphNode.START to a real node.
        node_name_set = set(names)
        has_entry = any(
            e.source == GraphNode.START and e.target in node_name_set for e in self.edges
        )
        if not has_entry:
            raise ValueError(
                f"GraphSpec must declare at least one entry edge from "
                f"{GraphNode.START!r} to a real node. Found edges: "
                f"{[(e.source, e.target) for e in self.edges]}."
            )

        # Edge endpoints: either sentinels or declared nodes.
        sentinels = {GraphNode.START, GraphNode.END}
        for edge in self.edges:
            for endpoint in (edge.source, edge.target):
                if endpoint not in sentinels and endpoint not in node_name_set:
                    raise ValueError(
                        f"Edge ({edge.source!r} → {edge.target!r}) references "
                        f"unknown node {endpoint!r}. All non-sentinel edge "
                        f"endpoints must be declared in `nodes`."
                    )
        return self


__all__ = ["EdgeSpec", "GraphSpec", "NodeSpec"]
