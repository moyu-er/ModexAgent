# ruff: noqa: ANN401
"""Tests for `HumanInputNode` + `HumanInputNodeFactory` (ticket 02 / P2.10).

Covers:

- Node construction (prompt + next_node).
- First entry: `execute()` raises `GraphInterrupt` with the prompt payload.
- `_execute` propagates `GraphInterrupt` (never swallowed).
- Pending-input path: the last payload's content is delivered downstream.
- `HumanInputNodeFactory`: creates from config.
- Factory rejects bad config (non-string prompt, non-string next_node).
- Factory `config_schema()` returns a Pydantic model.
- Integration with `NodeRegistry`.
"""

from __future__ import annotations

from typing import Any

import pytest
from helpers import make_ctx  # type: ignore[import-not-found]
from pydantic import BaseModel, ValidationError

from modex_graph import (
    GraphInterrupt,
    HumanInputNode,
    HumanInputNodeFactory,
    IntegratedInput,
    Node,
    NodeFactory,
    NodeRegistry,
    NodeSpec,
    NodeTrigger,
)
from modex_graph.nodes import HumanInputNodeConfig


def _delivered(ctx: Any, target: str) -> list[Any]:
    store = ctx.coordinator.get_deliver_store(target)
    if store is None:
        return []
    return [record.content for record in store.query_consumable(0, target)]


def _queue_input(ctx: Any, node: HumanInputNode, content: Any) -> None:
    ctx.coordinator.route_deliver(
        target_node_id=node.node_id,
        content=content,
        source_node_id="human",
        source_invocation_id=1,
    )

# ── HumanInputNode construction ───────────────────────────────────────────


class TestHumanInputNodeConstruction:
    def test_construction(self) -> None:
        node = HumanInputNode("Enter your name:", next_node="target")
        assert isinstance(node, Node)
        assert node._prompt == "Enter your name:"
        assert node._next_node == "target"

    def test_next_node_defaults_to_none(self) -> None:
        node = HumanInputNode("prompt")
        assert node._next_node is None

    def test_name_defaults_to_empty(self) -> None:
        node = HumanInputNode("prompt")
        assert node.name == ""

    def test_empty_prompt_allowed(self) -> None:
        node = HumanInputNode("")
        assert node._prompt == ""


# ── First entry: interrupt ────────────────────────────────────────────────


class TestHumanInputNodeInterrupt:
    async def test_first_entry_raises_graph_interrupt(self) -> None:
        node = HumanInputNode("Please approve:", next_node="target")
        node.name = "human_input"
        ctx = make_ctx()
        with pytest.raises(GraphInterrupt) as exc_info:
            await node.run(ctx)
        payload = exc_info.value.value
        assert isinstance(payload, dict)
        assert payload["prompt"] == "Please approve:"
        assert payload["node"] == "human_input"

    async def test_direct_execute_raises_graph_interrupt(self) -> None:
        node = HumanInputNode("Approve?", next_node="target")
        node.name = "human_input"
        ctx = make_ctx()
        with pytest.raises(GraphInterrupt):
            await node.execute(ctx, IntegratedInput())

    async def test_interrupt_does_not_deliver(self) -> None:
        node = HumanInputNode("Approve?", next_node="target")
        node.name = "human_input"
        ctx = make_ctx()
        with pytest.raises(GraphInterrupt):
            await node.run(ctx)
        assert _delivered(ctx, "target") == []


# ── Pending input: deliver ────────────────────────────────────────────────


class TestHumanInputNodePendingInput:
    async def test_delivers_last_payload_content(self) -> None:
        node = HumanInputNode("Approve?", next_node="downstream")
        node.name = "human_input"
        ctx = make_ctx()
        _queue_input(ctx, node, "first answer")
        _queue_input(ctx, node, {"approved": True})
        await node.run(ctx)
        assert _delivered(ctx, "downstream") == [{"approved": True}]

    async def test_pending_input_returns_none(self) -> None:
        node = HumanInputNode("Approve?", next_node="target")
        node.name = "human_input"
        ctx = make_ctx()
        _queue_input(ctx, node, "approved")
        result = await node.run(ctx)
        assert result is None

    async def test_delivers_payload_to_end(self) -> None:
        from modex_graph import GraphNode

        node = HumanInputNode("Approve?", next_node=GraphNode.END)
        node.name = "human_input"
        ctx = make_ctx()
        _queue_input(ctx, node, "approved")
        await node.run(ctx)
        assert _delivered(ctx, GraphNode.END) == ["approved"]


# ── HumanInputNodeFactory ─────────────────────────────────────────────────


class TestHumanInputNodeFactory:
    def test_create_from_config(self) -> None:
        factory = HumanInputNodeFactory()
        node = factory.create(
            NodeSpec(
                name="n1",
                node_type="human_input",
                config={"prompt": "Enter input:", "next_node": "out"},
            )
        )
        assert isinstance(node, HumanInputNode)
        assert node._prompt == "Enter input:"
        assert node._next_node == "out"

    def test_create_without_next_node(self) -> None:
        factory = HumanInputNodeFactory()
        node = factory.create(NodeSpec(name="n1", node_type="human_input", config={"prompt": "hi"}))
        assert isinstance(node, HumanInputNode)
        assert node._next_node is None

    def test_create_default_prompt_is_empty(self) -> None:
        factory = HumanInputNodeFactory()
        node = factory.create(NodeSpec(name="n1", node_type="human_input", config={}))
        assert isinstance(node, HumanInputNode)
        assert node._prompt == ""

    def test_create_returns_node_subclass(self) -> None:
        factory = HumanInputNodeFactory()
        node = factory.create(NodeSpec(name="n1", node_type="human_input", config={}))
        assert isinstance(node, Node)

    def test_config_schema_returns_model(self) -> None:
        factory = HumanInputNodeFactory()
        schema = factory.config_schema()
        assert schema is not None
        assert issubclass(schema, BaseModel)
        assert schema is HumanInputNodeConfig

    def test_factory_is_node_factory_subclass(self) -> None:
        assert issubclass(HumanInputNodeFactory, NodeFactory)


# ── Factory rejects bad config ────────────────────────────────────────────


class TestHumanInputNodeFactoryRejectsBadConfig:
    def test_non_string_prompt_raises(self) -> None:
        factory = HumanInputNodeFactory()
        with pytest.raises(ValidationError, match="prompt"):
            factory.create(NodeSpec(name="n1", node_type="human_input", config={"prompt": 123}))

    def test_list_prompt_raises(self) -> None:
        factory = HumanInputNodeFactory()
        with pytest.raises(ValidationError, match="prompt"):
            factory.create(
                NodeSpec(name="n1", node_type="human_input", config={"prompt": ["a", "b"]})
            )

    def test_non_string_next_node_raises(self) -> None:
        factory = HumanInputNodeFactory()
        with pytest.raises(ValidationError, match="next_node"):
            factory.create(
                NodeSpec(
                    name="n1",
                    node_type="human_input",
                    config={"prompt": "hi", "next_node": 123},
                )
            )

    def test_extra_config_key_rejected(self) -> None:
        factory = HumanInputNodeFactory()
        with pytest.raises(ValidationError, match="extra"):
            factory.create(
                NodeSpec(
                    name="n1",
                    node_type="human_input",
                    config={"prompt": "hi", "unknown_key": "bad"},
                )
            )


# ── Integration with NodeRegistry ─────────────────────────────────────────


class TestHumanInputNodeRegistryIntegration:
    def test_registry_sets_name_from_spec(self) -> None:
        registry = NodeRegistry()
        registry.register("human_input", HumanInputNodeFactory())
        node = registry.create(
            NodeSpec(name="my_input", node_type="human_input", config={"prompt": "hi"})
        )
        assert isinstance(node, HumanInputNode)
        assert node.name == "my_input"

    def test_registry_applies_trigger_override(self) -> None:
        registry = NodeRegistry()
        registry.register("human_input", HumanInputNodeFactory())
        node = registry.create(
            NodeSpec(
                name="my_input",
                node_type="human_input",
                config={"prompt": "hi"},
                trigger=NodeTrigger.ON_RECEIVE,
            )
        )
        assert node.trigger == NodeTrigger.ON_RECEIVE

    async def test_registry_create_then_interrupt(self) -> None:
        registry = NodeRegistry()
        registry.register("human_input", HumanInputNodeFactory())
        node = registry.create(
            NodeSpec(
                name="input_node",
                node_type="human_input",
                config={"prompt": "Approve?", "next_node": "out"},
            )
        )
        assert isinstance(node, HumanInputNode)
        ctx = make_ctx()
        with pytest.raises(GraphInterrupt) as exc_info:
            await node.run(ctx)
        payload = exc_info.value.value
        assert isinstance(payload, dict)
        assert payload["node"] == "input_node"

    async def test_registry_create_with_input_then_deliver(self) -> None:
        registry = NodeRegistry()
        registry.register("human_input", HumanInputNodeFactory())
        node = registry.create(
            NodeSpec(
                name="input_node",
                node_type="human_input",
                config={"prompt": "Approve?", "next_node": "out"},
            )
        )
        assert isinstance(node, HumanInputNode)
        ctx = make_ctx()
        _queue_input(ctx, node, "approved")
        await node.run(ctx)
        assert _delivered(ctx, "out") == ["approved"]
