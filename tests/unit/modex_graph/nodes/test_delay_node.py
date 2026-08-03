# ruff: noqa: ANN401
"""Tests for `DelayNode` + `DelayNodeFactory` (ticket 02 / P2.9).

Covers:

- Node construction (delay_seconds + next_node).
- `execute()` sleeps then delivers a tick signal.
- `_execute` orchestration: integrate -> execute -> submit.
- Zero-delay path (no sleep — fast for tests).
- `DelayNodeFactory`: creates from config.
- Factory rejects bad config (negative delay, non-numeric delay).
- Factory `config_schema()` returns a Pydantic model.
- Integration with `NodeRegistry`.
"""

from __future__ import annotations

import asyncio

import pytest
from helpers import make_ctx  # type: ignore[import-not-found]
from pydantic import BaseModel, ValidationError

from modex_graph import (
    DelayNode,
    DelayNodeFactory,
    GraphNode,
    Node,
    NodeFactory,
    NodeRegistry,
    NodeResult,
    NodeSpec,
    NodeTrigger,
)
from modex_graph.nodes import DelayNodeConfig

# ── DelayNode construction ────────────────────────────────────────────────


class TestDelayNodeConstruction:
    def test_construction(self) -> None:
        node = DelayNode(0.0, next_node="target")
        assert isinstance(node, Node)
        assert node._delay == 0.0
        assert node._next_node == "target"

    def test_next_node_defaults_to_none(self) -> None:
        node = DelayNode(0.0)
        assert node._next_node is None

    def test_name_defaults_to_empty(self) -> None:
        node = DelayNode(0.0)
        assert node.name == ""

    def test_negative_delay_raises(self) -> None:
        with pytest.raises(ValueError, match="must be >= 0"):
            DelayNode(-1.0)


# ── execute + deliver ─────────────────────────────────────────────────────


class TestDelayNodeExecute:
    async def test_zero_delay_delivers_tick(self) -> None:
        node = DelayNode(0.0, next_node="downstream")
        node.name = "delay_node"
        ctx = make_ctx()
        await node.run(ctx)
        assert node._submit_result == {"downstream": [{"delayed_seconds": 0.0}]}

    async def test_positive_delay_delivers_after_sleep(self) -> None:
        node = DelayNode(0.01, next_node="tick")
        node.name = "delay_node"
        ctx = make_ctx()
        await node.run(ctx)
        assert "tick" in node._submit_result
        assert node._submit_result["tick"] == [{"delayed_seconds": 0.01}]

    async def test_execute_returns_node_result(self) -> None:
        node = DelayNode(0.0, next_node="target")
        node.name = "delay_node"
        ctx = make_ctx()
        result = await node.run(ctx)
        assert isinstance(result, NodeResult)

    async def test_deliver_to_end_sentinel(self) -> None:
        node = DelayNode(0.0, next_node=GraphNode.END)
        node.name = "delay_node"
        ctx = make_ctx()
        await node.run(ctx)
        assert GraphNode.END in node._submit_result

    async def test_delay_actually_sleeps(self) -> None:
        node = DelayNode(0.05, next_node="tick")
        node.name = "delay_node"
        ctx = make_ctx()
        loop = asyncio.get_event_loop()
        start = loop.time()
        await node.run(ctx)
        elapsed = loop.time() - start
        assert elapsed >= 0.04


# ── DelayNodeFactory ──────────────────────────────────────────────────────


class TestDelayNodeFactory:
    def test_create_from_config(self) -> None:
        factory = DelayNodeFactory()
        node = factory.create(
            NodeSpec(
                name="n1",
                node_type="delay",
                config={"delay_seconds": 0.5, "next_node": "out"},
            )
        )
        assert isinstance(node, DelayNode)
        assert node._delay == 0.5
        assert node._next_node == "out"

    def test_create_without_next_node(self) -> None:
        factory = DelayNodeFactory()
        node = factory.create(NodeSpec(name="n1", node_type="delay", config={"delay_seconds": 0.0}))
        assert isinstance(node, DelayNode)
        assert node._next_node is None

    def test_create_default_delay_is_zero(self) -> None:
        factory = DelayNodeFactory()
        node = factory.create(NodeSpec(name="n1", node_type="delay", config={}))
        assert isinstance(node, DelayNode)
        assert node._delay == 0.0

    def test_create_int_delay_coerced_to_float(self) -> None:
        factory = DelayNodeFactory()
        node = factory.create(NodeSpec(name="n1", node_type="delay", config={"delay_seconds": 2}))
        assert isinstance(node, DelayNode)
        assert node._delay == 2.0
        assert isinstance(node._delay, float)

    def test_create_returns_node_subclass(self) -> None:
        factory = DelayNodeFactory()
        node = factory.create(NodeSpec(name="n1", node_type="delay", config={}))
        assert isinstance(node, Node)

    def test_config_schema_returns_model(self) -> None:
        factory = DelayNodeFactory()
        schema = factory.config_schema()
        assert schema is not None
        assert issubclass(schema, BaseModel)
        assert schema is DelayNodeConfig

    def test_factory_is_node_factory_subclass(self) -> None:
        assert issubclass(DelayNodeFactory, NodeFactory)


# ── Factory rejects bad config ────────────────────────────────────────────


class TestDelayNodeFactoryRejectsBadConfig:
    def test_negative_delay_raises(self) -> None:
        factory = DelayNodeFactory()
        with pytest.raises(ValueError, match="must be >= 0"):
            factory.create(NodeSpec(name="n1", node_type="delay", config={"delay_seconds": -0.5}))

    def test_non_numeric_delay_raises(self) -> None:
        factory = DelayNodeFactory()
        with pytest.raises(ValidationError, match="delay_seconds"):
            factory.create(
                NodeSpec(name="n1", node_type="delay", config={"delay_seconds": "not_a_number"})
            )

    def test_list_delay_raises(self) -> None:
        factory = DelayNodeFactory()
        with pytest.raises(ValidationError, match="delay_seconds"):
            factory.create(NodeSpec(name="n1", node_type="delay", config={"delay_seconds": [1, 2]}))

    def test_non_string_next_node_raises(self) -> None:
        factory = DelayNodeFactory()
        with pytest.raises(ValidationError, match="next_node"):
            factory.create(
                NodeSpec(
                    name="n1",
                    node_type="delay",
                    config={"delay_seconds": 0.0, "next_node": 123},
                )
            )

    def test_extra_config_key_rejected(self) -> None:
        factory = DelayNodeFactory()
        with pytest.raises(ValidationError, match="extra"):
            factory.create(
                NodeSpec(
                    name="n1",
                    node_type="delay",
                    config={"delay_seconds": 0.0, "unknown_key": "bad"},
                )
            )


# ── Integration with NodeRegistry ─────────────────────────────────────────


class TestDelayNodeRegistryIntegration:
    def test_registry_sets_name_from_spec(self) -> None:
        registry = NodeRegistry()
        registry.register("delay", DelayNodeFactory())
        node = registry.create(
            NodeSpec(name="my_delay", node_type="delay", config={"delay_seconds": 0.0})
        )
        assert isinstance(node, DelayNode)
        assert node.name == "my_delay"

    def test_registry_applies_trigger_override(self) -> None:
        registry = NodeRegistry()
        registry.register("delay", DelayNodeFactory())
        node = registry.create(
            NodeSpec(
                name="my_delay",
                node_type="delay",
                config={"delay_seconds": 0.0},
                trigger=NodeTrigger.ON_RECEIVE,
            )
        )
        assert node.trigger == NodeTrigger.ON_RECEIVE

    async def test_registry_create_then_execute(self) -> None:
        registry = NodeRegistry()
        registry.register("delay", DelayNodeFactory())
        node = registry.create(
            NodeSpec(
                name="delay_node",
                node_type="delay",
                config={"delay_seconds": 0.0, "next_node": "out"},
            )
        )
        ctx = make_ctx()
        await node.run(ctx)
        assert node._submit_result == {"out": [{"delayed_seconds": 0.0}]}
