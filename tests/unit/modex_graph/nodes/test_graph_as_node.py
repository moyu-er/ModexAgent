# ruff: noqa: ANN401
"""Tests for `GraphAsNode` + `GraphAsNodeFactory` (ticket 02 / P2.8).

Covers:

- Node construction (wraps a `CompiledGraph`).
- `execute()` runs the inner graph and delivers a completion signal.
- `_execute` orchestration: the inner graph mutates `ctx.state`, the
  wrapper delivers `{"subgraph_completed": True}`.
- `GraphAsNodeFactory`: creates from an inline `GraphSpec` in config.
- Factory rejects bad config (missing `graph_spec`, wrong type).
- Factory `config_schema()` returns None.
- Integration with `NodeRegistry`.
"""

from __future__ import annotations

from typing import Any

import pytest
from helpers import CounterState, make_ctx  # type: ignore[import-not-found]
from pydantic import BaseModel

from modex_graph import (
    CompiledGraph,
    EdgeSpec,
    Graph,
    GraphAsNode,
    GraphAsNodeFactory,
    GraphContext,
    GraphNode,
    GraphSpec,
    GraphSpecCompiler,
    IntegratedInput,
    Node,
    NodeFactory,
    NodeRegistry,
    NodeResult,
    NodeSpec,
    StateFieldSpec,
    StateRegistry,
    StateSchema,
)

# ── Test fixtures: inner graph node + factory ────────────────────────────


class _IncNode(Node[CounterState]):
    def execute(self, ctx: GraphContext[CounterState], integrated_input: IntegratedInput) -> NodeResult:
        ctx.state.count += 1
        self.deliver(None, None, ctx)
        return NodeResult()


class _IncFactory(NodeFactory):
    def create(self, spec: NodeSpec) -> Node[Any]:
        return _IncNode()

    def config_schema(self) -> type[BaseModel] | None:
        return None


def _state_schema() -> StateSchema:
    return StateSchema(
        name="counter_state",
        fields=[
            StateFieldSpec(name="count", field_type="int", default=0),
            StateFieldSpec(name="name", field_type="str", default=""),
        ],
    )


def _inner_graph_spec() -> GraphSpec:
    return GraphSpec(
        name="inner_graph",
        nodes=[NodeSpec(name="inc", node_type="inc")],
        edges=[
            EdgeSpec(source=GraphNode.START, target="inc"),
            EdgeSpec(source="inc", target=GraphNode.END),
        ],
        state_schema=_state_schema(),
    )


def _compiler() -> GraphSpecCompiler:
    nodes = NodeRegistry()
    nodes.register("inc", _IncFactory())
    states = StateRegistry()
    return GraphSpecCompiler(nodes, states)


def _compiled_inner() -> CompiledGraph[Any]:
    inner: Graph[CounterState] = Graph(name="inner")
    inner.add_node("inc", _IncNode())
    inner.add_edge(GraphNode.START, "inc")
    inner.add_edge("inc", GraphNode.END)
    return inner.compile()


# ── GraphAsNode construction ──────────────────────────────────────────────


class TestGraphAsNodeConstruction:
    def test_construction(self) -> None:
        compiled = _compiled_inner()
        node = GraphAsNode(compiled, next_node="parent_target")
        assert isinstance(node, Node)
        assert node._compiled is compiled
        assert node._next_node == "parent_target"

    def test_next_node_defaults_to_none(self) -> None:
        node = GraphAsNode(_compiled_inner())
        assert node._next_node is None

    def test_name_defaults_to_empty(self) -> None:
        node = GraphAsNode(_compiled_inner())
        assert node.name == ""


# ── execute + deliver ─────────────────────────────────────────────────────


class TestGraphAsNodeExecute:
    async def test_execute_runs_inner_graph_and_delivers(self) -> None:
        node = GraphAsNode(_compiled_inner(), next_node="parent")
        node.name = "subgraph_node"
        ctx = make_ctx(CounterState(count=0, name=""))
        await node.run(ctx)
        assert "parent" in node._submit_result
        delivered = node._submit_result["parent"]
        assert len(delivered) == 1
        assert delivered[0] == {"subgraph_completed": True}

    async def test_inner_graph_mutates_shared_state(self) -> None:
        node = GraphAsNode(_compiled_inner(), next_node="parent")
        node.name = "subgraph_node"
        ctx = make_ctx(CounterState(count=5, name=""))
        await node.run(ctx)
        assert ctx.state.count == 6

    async def test_execute_returns_node_result(self) -> None:
        node = GraphAsNode(_compiled_inner(), next_node="parent")
        node.name = "subgraph_node"
        ctx = make_ctx()
        result = await node.run(ctx)
        assert isinstance(result, NodeResult)

    async def test_deliver_to_end_sentinel(self) -> None:
        node = GraphAsNode(_compiled_inner(), next_node=GraphNode.END)
        node.name = "subgraph_node"
        ctx = make_ctx()
        await node.run(ctx)
        assert GraphNode.END in node._submit_result


# ── GraphAsNodeFactory ────────────────────────────────────────────────────


class TestGraphAsNodeFactory:
    def test_create_from_dict_config(self) -> None:
        factory = GraphAsNodeFactory(_compiler())
        spec = NodeSpec(
            name="sub",
            node_type="graph_as_node",
            config={
                "graph_spec": _inner_graph_spec().model_dump(),
                "next_node": "parent_target",
            },
        )
        node = factory.create(spec)
        assert isinstance(node, GraphAsNode)
        assert isinstance(node._compiled, CompiledGraph)
        assert node._next_node == "parent_target"

    def test_create_from_graph_spec_object(self) -> None:
        factory = GraphAsNodeFactory(_compiler())
        spec = NodeSpec(
            name="sub",
            node_type="graph_as_node",
            config={"graph_spec": _inner_graph_spec(), "next_node": "out"},
        )
        node = factory.create(spec)
        assert isinstance(node, GraphAsNode)

    def test_create_without_next_node(self) -> None:
        factory = GraphAsNodeFactory(_compiler())
        spec = NodeSpec(
            name="sub",
            node_type="graph_as_node",
            config={"graph_spec": _inner_graph_spec().model_dump()},
        )
        node = factory.create(spec)
        assert isinstance(node, GraphAsNode)
        assert node._next_node is None

    def test_create_returns_node_subclass(self) -> None:
        factory = GraphAsNodeFactory(_compiler())
        node = factory.create(
            NodeSpec(
                name="sub",
                node_type="graph_as_node",
                config={"graph_spec": _inner_graph_spec().model_dump()},
            )
        )
        assert isinstance(node, Node)

    def test_config_schema_returns_none(self) -> None:
        factory = GraphAsNodeFactory(_compiler())
        assert factory.config_schema() is None

    def test_factory_is_node_factory_subclass(self) -> None:
        assert issubclass(GraphAsNodeFactory, NodeFactory)


# ── Factory rejects bad config ────────────────────────────────────────────


class TestGraphAsNodeFactoryRejectsBadConfig:
    def test_missing_graph_spec_key(self) -> None:
        factory = GraphAsNodeFactory(_compiler())
        with pytest.raises(ValueError, match="requires a 'graph_spec'"):
            factory.create(NodeSpec(name="sub", node_type="graph_as_node", config={}))

    def test_wrong_type_graph_spec(self) -> None:
        factory = GraphAsNodeFactory(_compiler())
        with pytest.raises(ValueError, match="must be a dict or GraphSpec"):
            factory.create(
                NodeSpec(
                    name="sub",
                    node_type="graph_as_node",
                    config={"graph_spec": 123},
                )
            )

    def test_non_string_next_node(self) -> None:
        factory = GraphAsNodeFactory(_compiler())
        with pytest.raises(ValueError, match="next_node"):
            factory.create(
                NodeSpec(
                    name="sub",
                    node_type="graph_as_node",
                    config={
                        "graph_spec": _inner_graph_spec().model_dump(),
                        "next_node": 123,
                    },
                )
            )

    def test_invalid_graph_spec_data_raises(self) -> None:
        from pydantic import ValidationError

        factory = GraphAsNodeFactory(_compiler())
        with pytest.raises((ValidationError, ValueError)):
            factory.create(
                NodeSpec(
                    name="sub",
                    node_type="graph_as_node",
                    config={
                        "graph_spec": {"name": "bad", "nodes": [], "edges": [], "state_schema": "x"}
                    },
                )
            )


# ── Integration with NodeRegistry ─────────────────────────────────────────


class TestGraphAsNodeRegistryIntegration:
    def test_registry_sets_name_from_spec(self) -> None:
        registry = NodeRegistry()
        registry.register("graph_as_node", GraphAsNodeFactory(_compiler()))
        node = registry.create(
            NodeSpec(
                name="my_subgraph",
                node_type="graph_as_node",
                config={"graph_spec": _inner_graph_spec().model_dump()},
            )
        )
        assert isinstance(node, GraphAsNode)
        assert node.name == "my_subgraph"

    async def test_registry_create_then_execute(self) -> None:
        registry = NodeRegistry()
        registry.register("graph_as_node", GraphAsNodeFactory(_compiler()))
        node = registry.create(
            NodeSpec(
                name="sub",
                node_type="graph_as_node",
                config={
                    "graph_spec": _inner_graph_spec().model_dump(),
                    "next_node": "parent",
                },
            )
        )
        ctx = make_ctx(CounterState(count=3, name=""))
        await node.run(ctx)
        assert ctx.state.count == 4
        assert "parent" in node._submit_result
