# ruff: noqa: ANN401
"""Tests for `FunctionNode` + `FunctionNodeFactory` (ticket 02 / P2.7).

Covers:

- Node construction (sync + async function wrappers).
- `execute()` calls `deliver()` with the function's return value.
- `_execute` orchestration: integrate -> execute -> submit.
- Sync and async function dual-mode (inspect.isawaitable).
- `FunctionNodeFactory`: creates from config, holds a function registry.
- Factory rejects bad config (missing/unregistered/non-string function name).
- Factory `register_function` / `unregister_function` lifecycle.
- Factory `config_schema()` returns a Pydantic model.
- Integration with `NodeRegistry` (name + trigger set by registry).
"""

from __future__ import annotations

from typing import Any

import pytest
from helpers import CounterState, make_ctx  # type: ignore[import-not-found]
from pydantic import BaseModel, ValidationError

from modex_graph import (
    FunctionNode,
    FunctionNodeFactory,
    GraphContext,
    Node,
    NodeFactory,
    NodeRegistry,
    NodeResult,
    NodeSpec,
    NodeTrigger,
)
from modex_graph.nodes import FunctionNodeConfig

# ── Test functions ────────────────────────────────────────────────────────


def _sync_double(ctx: GraphContext[Any]) -> int:
    return 42


async def _async_greet(ctx: GraphContext[Any]) -> str:
    return "hello"


def _read_state(ctx: GraphContext[CounterState]) -> int:
    return ctx.state.count


# ── FunctionNode construction ─────────────────────────────────────────────


class TestFunctionNodeConstruction:
    def test_sync_function_construction(self) -> None:
        node = FunctionNode(_sync_double, next_node="target")
        assert isinstance(node, Node)
        assert node._func is _sync_double
        assert node._next_node == "target"

    def test_async_function_construction(self) -> None:
        node = FunctionNode(_async_greet, next_node="target")
        assert isinstance(node, Node)

    def test_next_node_defaults_to_none(self) -> None:
        node = FunctionNode(_sync_double)
        assert node._next_node is None

    def test_name_defaults_to_empty(self) -> None:
        node = FunctionNode(_sync_double)
        assert node.name == ""


# ── execute + deliver ─────────────────────────────────────────────────────


class TestFunctionNodeExecute:
    async def test_sync_function_delivers_result(self) -> None:
        node = FunctionNode(_sync_double, next_node="downstream")
        node.name = "sync_fn"
        ctx = make_ctx(CounterState(count=0))
        await node.run(ctx)
        assert node._submit_result == {"downstream": [42]}

    async def test_async_function_delivers_result(self) -> None:
        node = FunctionNode(_async_greet, next_node="greet_target")
        node.name = "async_fn"
        ctx = make_ctx()
        await node.run(ctx)
        assert node._submit_result == {"greet_target": ["hello"]}

    async def test_function_reads_state(self) -> None:
        node = FunctionNode(_read_state, next_node="state_target")
        node.name = "read_state"
        ctx = make_ctx(CounterState(count=7))
        await node.run(ctx)
        assert node._submit_result == {"state_target": [7]}

    async def test_execute_returns_node_result(self) -> None:
        node = FunctionNode(_sync_double, next_node="target")
        node.name = "fn"
        ctx = make_ctx()
        result = await node.run(ctx)
        assert isinstance(result, NodeResult)

    async def test_deliver_to_end_sentinel(self) -> None:
        from modex_graph import GraphNode

        node = FunctionNode(_sync_double, next_node=GraphNode.END)
        node.name = "fn_end"
        ctx = make_ctx()
        await node.run(ctx)
        assert GraphNode.END in node._submit_result


# ── FunctionNodeFactory ───────────────────────────────────────────────────


class TestFunctionNodeFactory:
    def test_create_from_config(self) -> None:
        factory = FunctionNodeFactory({"double": _sync_double})
        node = factory.create(
            NodeSpec(
                name="n1", node_type="function", config={"function": "double", "next_node": "out"}
            )
        )
        assert isinstance(node, FunctionNode)
        assert node._func is _sync_double
        assert node._next_node == "out"

    def test_create_without_next_node(self) -> None:
        factory = FunctionNodeFactory({"double": _sync_double})
        node = factory.create(
            NodeSpec(name="n1", node_type="function", config={"function": "double"})
        )
        assert isinstance(node, FunctionNode)
        assert node._next_node is None

    def test_create_async_function(self) -> None:
        factory = FunctionNodeFactory({"greet": _async_greet})
        node = factory.create(
            NodeSpec(name="n1", node_type="function", config={"function": "greet"})
        )
        assert isinstance(node, FunctionNode)
        assert node._func is _async_greet

    def test_create_returns_node_subclass(self) -> None:
        factory = FunctionNodeFactory({"double": _sync_double})
        node = factory.create(
            NodeSpec(name="n1", node_type="function", config={"function": "double"})
        )
        assert isinstance(node, Node)

    def test_register_function(self) -> None:
        factory = FunctionNodeFactory()
        factory.register_function("triple", _sync_double)
        node = factory.create(
            NodeSpec(name="n1", node_type="function", config={"function": "triple"})
        )
        assert isinstance(node, FunctionNode)
        assert node._func is _sync_double

    def test_register_function_duplicate_raises(self) -> None:
        factory = FunctionNodeFactory({"double": _sync_double})
        with pytest.raises(ValueError, match="already registered"):
            factory.register_function("double", _sync_double)

    def test_unregister_function(self) -> None:
        factory = FunctionNodeFactory({"double": _sync_double})
        factory.unregister_function("double")
        with pytest.raises(ValueError, match="not registered"):
            factory.create(NodeSpec(name="n1", node_type="function", config={"function": "double"}))

    def test_unregister_noop_if_not_registered(self) -> None:
        factory = FunctionNodeFactory()
        factory.unregister_function("nonexistent")

    def test_config_schema_returns_model(self) -> None:
        factory = FunctionNodeFactory()
        schema = factory.config_schema()
        assert schema is not None
        assert issubclass(schema, BaseModel)
        assert schema is FunctionNodeConfig

    def test_empty_registry_by_default(self) -> None:
        factory = FunctionNodeFactory()
        with pytest.raises(ValueError, match="not registered"):
            factory.create(
                NodeSpec(name="n1", node_type="function", config={"function": "anything"})
            )

    def test_initial_functions_dict_is_copied(self) -> None:
        original: dict[str, Any] = {"double": _sync_double}
        factory = FunctionNodeFactory(original)
        factory.unregister_function("double")
        assert "double" in original

    def test_factory_is_node_factory_subclass(self) -> None:
        assert issubclass(FunctionNodeFactory, NodeFactory)


# ── Factory rejects bad config ────────────────────────────────────────────


class TestFunctionNodeFactoryRejectsBadConfig:
    def test_missing_function_key(self) -> None:
        factory = FunctionNodeFactory({"double": _sync_double})
        with pytest.raises(ValidationError, match="function"):
            factory.create(NodeSpec(name="n1", node_type="function", config={}))

    def test_unregistered_function_name(self) -> None:
        factory = FunctionNodeFactory({"double": _sync_double})
        with pytest.raises(ValueError, match="not registered"):
            factory.create(
                NodeSpec(name="n1", node_type="function", config={"function": "nonexistent"})
            )

    def test_non_string_function_name(self) -> None:
        factory = FunctionNodeFactory({"double": _sync_double})
        with pytest.raises(ValidationError, match="function"):
            factory.create(NodeSpec(name="n1", node_type="function", config={"function": 123}))

    def test_non_string_next_node(self) -> None:
        factory = FunctionNodeFactory({"double": _sync_double})
        with pytest.raises(ValidationError, match="next_node"):
            factory.create(
                NodeSpec(
                    name="n1",
                    node_type="function",
                    config={"function": "double", "next_node": 123},
                )
            )

    def test_extra_config_key_rejected(self) -> None:
        factory = FunctionNodeFactory({"double": _sync_double})
        with pytest.raises(ValidationError, match="extra"):
            factory.create(
                NodeSpec(
                    name="n1",
                    node_type="function",
                    config={"function": "double", "unknown_key": "bad"},
                )
            )


# ── Integration with NodeRegistry ─────────────────────────────────────────


class TestFunctionNodeRegistryIntegration:
    def test_registry_sets_name_from_spec(self) -> None:
        registry = NodeRegistry()
        registry.register("function", FunctionNodeFactory({"double": _sync_double}))
        node = registry.create(
            NodeSpec(name="my_fn", node_type="function", config={"function": "double"})
        )
        assert isinstance(node, FunctionNode)
        assert node.name == "my_fn"

    def test_registry_applies_trigger_override(self) -> None:
        registry = NodeRegistry()
        registry.register("function", FunctionNodeFactory({"double": _sync_double}))
        node = registry.create(
            NodeSpec(
                name="my_fn",
                node_type="function",
                config={"function": "double"},
                trigger=NodeTrigger.ON_RECEIVE,
            )
        )
        assert node.trigger == NodeTrigger.ON_RECEIVE

    async def test_registry_create_then_execute(self) -> None:
        registry = NodeRegistry()
        registry.register("function", FunctionNodeFactory({"double": _sync_double}))
        node = registry.create(
            NodeSpec(
                name="fn_node",
                node_type="function",
                config={"function": "double", "next_node": "out"},
            )
        )
        ctx = make_ctx()
        await node.run(ctx)
        assert node._submit_result == {"out": [42]}
