# ruff: noqa: ANN401
"""`GraphSpec` + `NodeSpec` + `EdgeSpec` — declarative graph specification.

`GraphSpec` is the declarative, fully-serializable graph
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

    The deliver/submit model replaces the transition model.
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


class FieldSpec(BaseModel):
    """Declarative shape of one state field — the envelope of a state schema.

    `FieldSpec` describes ONLY the field shape: its type, optional item type
    for list fields, and an optional initial value. The dict key in
    `GraphSpec.state_schema` IS the field name — `FieldSpec` deliberately
    has NO `name` field (SPEC §8.2 revised: "FieldSpec 无 name 字段，dict
    键即名").

    `type` is a plain `str` (not an enum): modex_graph is framework-agnostic
    and cannot know the valid type universe. The injected
    `state_schema_compiler` (on `GraphSpecCompiler`) resolves type names —
    built-in primitives like `"string"`/`"int"`/`"list"` plus custom types
    resolved from `DATA_NAMESPACE` on the modex_agent side (Task 27).

    Fields:
    - `type`: type name string (e.g. `"string"`, `"int"`, `"list"`,
      `"my_plugin_data_type"`). Validated by the injected compiler, not here.
    - `item_type`: for `type == "list"`, the element type name. `None` for
      non-list fields or untyped lists.
    - `initial`: optional initial value. `None` means "no initial value
      specified" (the compiler decides the default).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str
    item_type: str | None = None
    initial: Any | None = None


class GraphSpec(BaseModel):
    """Declarative graph specification — fully serializable, the persistence unit.

    `GraphSpec` is what gets persisted to the `graph_specs`
    table. The full chain is
    `GraphSpec → GraphSpecCompiler → CompiledGraph → GraphInstance →
    GraphEngine`. `GraphSpecCompiler` and `TopologyValidator` are P2 (out
    of scope here).

    State is declared in ONE of two mutually exclusive ways:

    - `state_class: str | None` — names a `GraphState` subclass registered
      in the compiler's injected state-class mapping (existing path).
    - `state_schema: dict[str, FieldSpec] | None` — declarative field
      shapes; the compiler's injected `state_schema_compiler` resolves the
      schema into a dynamic `GraphState` subclass (SPEC §8.2). modex_graph
      only carries the envelope; the compiler lives on the modex_agent side
      (Task 27) so modex_graph stays framework-agnostic.

    Exactly one of `state_class` / `state_schema` must be set.

    Basic structural validation is done here (no duplicate node names, at
    least one node, at least one entry edge from `GraphNode.START`). Full
    topology validation (cycle detection, reachability, max_depth) is
    `TopologyValidator` in P2.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    nodes: list[NodeSpec] = Field(default_factory=list)
    edges: list[EdgeSpec] = Field(default_factory=list)
    state_class: str | None = None
    state_schema: dict[str, FieldSpec] | None = None
    scheduler: SchedulerKind = SchedulerKind.LINEAR
    version: str = "1.0"
    metadata: dict[str, Any] = Field(default_factory=dict)
    max_iterations: int = 25
    default_trigger: NodeTrigger = NodeTrigger.ON_ALL_PREDS

    @model_validator(mode="after")
    def _validate_state_exclusivity(self) -> GraphSpec:
        """`state_class` and `state_schema` are mutually exclusive; one required."""
        if self.state_schema is not None and self.state_class is not None:
            raise ValueError(
                "GraphSpec.state_schema and state_class are mutually exclusive. "
                "Use state_schema for declarative state shape (compiled via "
                "state_schema_compiler) OR state_class for a registered "
                "GraphState subclass name."
            )
        if self.state_schema is None and self.state_class is None:
            raise ValueError(
                "GraphSpec must specify either state_class (registered name) "
                "or state_schema (declarative field shape)."
            )
        return self

    @model_validator(mode="after")
    def _validate_structure(self) -> GraphSpec:
        """Basic structural checks (full topology validation is P2).

        - No duplicate node names.
        - At least one entry edge from `GraphNode.START`.
        - `max_iterations` > 0.
        - Edge endpoints are either sentinels or reference declared nodes.
        - `default_trigger` and per-node `trigger` must not be `ON_RECEIVE`
          (declarative API rejects deprecated triggers; use the imperative
          `Graph.compile()` API if you need ON_RECEIVE with a warning).
        """
        if self.default_trigger == NodeTrigger.ON_RECEIVE:
            raise ValueError(
                "GraphSpec.default_trigger=ON_RECEIVE is rejected in the "
                "declarative API. ON_RECEIVE is deprecated/experimental. "
                "Use NodeTrigger.ON_ALL_PREDS for production graphs."
            )
        for node in self.nodes:
            if node.trigger == NodeTrigger.ON_RECEIVE:
                raise ValueError(
                    f"NodeSpec {node.name!r} declares trigger=ON_RECEIVE, "
                    "which is rejected in the declarative API. ON_RECEIVE is "
                    "deprecated/experimental. Use NodeTrigger.ON_ALL_PREDS "
                    "for production graphs."
                )
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

        # At least one entry edge from GraphNode.START to a declared node or END.
        node_name_set = set(names)
        has_entry = any(
            e.source == GraphNode.START
            and (e.target == GraphNode.END or e.target in node_name_set)
            for e in self.edges
        )
        if not has_entry:
            raise ValueError(
                f"GraphSpec must declare at least one entry edge from "
                f"{GraphNode.START!r}. Found edges: "
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


__all__ = ["EdgeSpec", "FieldSpec", "GraphSpec", "NodeSpec"]
