# ruff: noqa: ANN401
"""Tests for `NodeFactory` ABC + `NodeRegistry`."""

from __future__ import annotations

import re
from typing import Any, cast

import pytest
from pydantic import BaseModel, ValidationError

from modex_graph import (
    Graph,
    GraphContext,
    GraphState,
    IntegratedInput,
    Node,
    NodeFactory,
    NodeRegistry,
    NodeSpec,
    NodeTrigger,
    generate_id,
)


class _EchoNode(Node[GraphState]):
    """Minimal Node for testing — holds a `message` from config."""

    message: str = "default"

    def __init__(self, message: str = "default") -> None:
        self.message = message

    async def execute(
        self,
        ctx: GraphContext[GraphState],
        integrated_input: IntegratedInput,
    ) -> None:
        return None


class _EchoConfig(BaseModel):
    message: str = "default"


class _EchoFactory(NodeFactory):
    """Factory that creates `_EchoNode` from a config with `message`."""

    def create(self, spec: NodeSpec) -> Node[Any]:
        message = spec.config.get("message", "default")
        return _EchoNode(message=str(message))

    def config_schema(self) -> type[BaseModel] | None:
        return _EchoConfig


class _NoSchemaFactory(NodeFactory):
    """Factory with no config validation."""

    def create(self, spec: NodeSpec) -> Node[Any]:
        return _EchoNode()

    def config_schema(self) -> type[BaseModel] | None:
        return None


def _echo(registry: NodeRegistry, spec: NodeSpec) -> _EchoNode:
    """Helper: create via registry and cast to `_EchoNode` for attribute access."""
    return cast(_EchoNode, registry.create(spec))


class TestNodeFactoryABC:
    """`NodeFactory` ABC — cannot be instantiated directly."""

    def test_abc_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            NodeFactory()  # type: ignore[abstract]

    def test_subclass_must_implement_both_methods(self) -> None:
        class _MissingCreate(NodeFactory):
            def config_schema(self) -> type[BaseModel] | None:
                return None

        with pytest.raises(TypeError):
            _MissingCreate()  # type: ignore[abstract]

        class _MissingSchema(NodeFactory):
            def create(self, spec: NodeSpec) -> Node[Any]:
                return _EchoNode()

        with pytest.raises(TypeError):
            _MissingSchema()  # type: ignore[abstract]


class TestNodeRegistry:
    """`NodeRegistry` — register factories, create nodes from specs."""

    def test_register_and_create(self) -> None:
        registry = NodeRegistry()
        registry.register("echo", _EchoFactory())
        assert registry.is_registered("echo")

        node = _echo(registry, NodeSpec(name="n1", node_type="echo", config={"message": "hi"}))
        assert isinstance(node, _EchoNode)
        assert node.message == "hi"
        assert node.name == "n1"

    def test_create_sets_node_name_from_spec(self) -> None:
        registry = NodeRegistry()
        registry.register("echo", _EchoFactory())

        node = registry.create(NodeSpec(name="my_node", node_type="echo"))
        assert node.name == "my_node"

    def test_create_sets_unique_sortable_node_ids(self) -> None:
        registry = NodeRegistry()
        registry.register("echo", _EchoFactory())

        first = registry.create(NodeSpec(name="first", node_type="echo"))
        second = registry.create(NodeSpec(name="second", node_type="echo"))

        assert re.fullmatch(r"node_[0-9a-f]{12}[0-9A-Za-z]{14}", first.node_id)
        assert re.fullmatch(r"node_[0-9a-f]{12}[0-9A-Za-z]{14}", second.node_id)
        assert first.node_id != second.node_id

    def test_graph_add_node_preserves_registry_assigned_id(self) -> None:
        registry = NodeRegistry()
        registry.register("echo", _EchoFactory())
        node = registry.create(NodeSpec(name="original", node_type="echo"))
        assigned_id = node.node_id
        graph: Graph[GraphState] = Graph()

        graph.add_node("renamed", node)

        assert node.node_id == assigned_id

    def test_create_applies_trigger_override(self) -> None:
        registry = NodeRegistry()
        registry.register("echo", _EchoFactory())

        node = registry.create(
            NodeSpec(name="n1", node_type="echo", trigger=NodeTrigger.ON_RECEIVE)
        )
        assert node.trigger == NodeTrigger.ON_RECEIVE

    def test_create_no_trigger_keeps_default(self) -> None:
        registry = NodeRegistry()
        registry.register("echo", _EchoFactory())

        node = registry.create(NodeSpec(name="n1", node_type="echo"))
        assert node.trigger is None

    def test_create_unknown_type_raises_keyerror(self) -> None:
        registry = NodeRegistry()
        with pytest.raises(KeyError, match="not registered"):
            registry.create(NodeSpec(name="n1", node_type="nonexistent"))

    def test_register_duplicate_raises_valueerror(self) -> None:
        registry = NodeRegistry()
        registry.register("echo", _EchoFactory())
        with pytest.raises(ValueError, match="already registered"):
            registry.register("echo", _EchoFactory())

    def test_unregister(self) -> None:
        registry = NodeRegistry()
        registry.register("echo", _EchoFactory())
        assert registry.is_registered("echo")
        registry.unregister("echo")
        assert not registry.is_registered("echo")
        registry.unregister("echo")

    def test_registered_types_sorted(self) -> None:
        registry = NodeRegistry()
        registry.register("zeta", _NoSchemaFactory())
        registry.register("alpha", _NoSchemaFactory())
        registry.register("mid", _NoSchemaFactory())
        assert registry.registered_types() == ["alpha", "end", "mid", "start", "zeta"]

    def test_no_schema_factory_skips_validation(self) -> None:
        registry = NodeRegistry()
        registry.register("plain", _NoSchemaFactory())

        node = registry.create(NodeSpec(name="n1", node_type="plain", config={"anything": True}))
        assert isinstance(node, _EchoNode)

    def test_config_validation_rejects_invalid(self) -> None:
        registry = NodeRegistry()
        registry.register("echo", _EchoFactory())

        # Pydantic v2 coerces int→str in lax mode, so use a list (which
        # cannot be coerced to str) to trigger a real ValidationError.
        with pytest.raises(ValidationError):
            registry.create(
                NodeSpec(name="n1", node_type="echo", config={"message": ["not", "a", "string"]})
            )

    def test_config_validation_accepts_valid(self) -> None:
        registry = NodeRegistry()
        registry.register("echo", _EchoFactory())

        node = _echo(registry, NodeSpec(name="n1", node_type="echo", config={"message": "hello"}))
        assert node.message == "hello"

    def test_config_validation_accepts_empty_when_optional(self) -> None:
        registry = NodeRegistry()
        registry.register("echo", _EchoFactory())

        node = _echo(registry, NodeSpec(name="n1", node_type="echo"))
        assert node.message == "default"

    def test_create_node_is_node_subclass(self) -> None:
        registry = NodeRegistry()
        registry.register("echo", _EchoFactory())

        node = registry.create(NodeSpec(name="n1", node_type="echo"))
        assert isinstance(node, Node)

    def test_no_schema_factory_no_config_model(self) -> None:
        assert _NoSchemaFactory().config_schema() is None


class TestGenerateId:
    def test_without_prefix_has_fixed_length_body(self) -> None:
        generated = generate_id()

        assert re.fullmatch(r"[0-9a-f]{12}[0-9A-Za-z]{14}", generated)

    def test_custom_separator_is_applied_to_trimmed_prefix(self) -> None:
        generated = generate_id(prefix=" node ", separator="-")

        assert re.fullmatch(r"node-[0-9a-f]{12}[0-9A-Za-z]{14}", generated)
