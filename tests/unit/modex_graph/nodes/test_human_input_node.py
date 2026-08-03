# ruff: noqa: ANN401
"""Tests for `HumanInputNode` + `HumanInputNodeFactory` (ticket 02 / P2.10).

Covers:

- Node construction (prompt + next_node).
- First entry: `execute()` raises `GraphInterrupt` with the prompt payload.
- `_execute` propagates `GraphInterrupt` (never swallowed).
- Resume path: setting `_resumed = True` before `_execute` causes the node
  to deliver a "human_input_resumed" signal instead of interrupting.
- `HumanInputNodeFactory`: creates from config.
- Factory rejects bad config (non-string prompt, non-string next_node).
- Factory `config_schema()` returns None.
- Integration with `NodeRegistry`.
"""

from __future__ import annotations

import pytest
from helpers import make_ctx  # type: ignore[import-not-found]

from modex_graph import (
    GraphInterrupt,
    HumanInputNode,
    HumanInputNodeFactory,
    IntegratedInput,
    Node,
    NodeFactory,
    NodeRegistry,
    NodeResult,
    NodeSpec,
    NodeTrigger,
)

# ── HumanInputNode construction ───────────────────────────────────────────


class TestHumanInputNodeConstruction:
    def test_construction(self) -> None:
        node = HumanInputNode("Enter your name:", next_node="target")
        assert isinstance(node, Node)
        assert node._prompt == "Enter your name:"
        assert node._next_node == "target"
        assert node._resumed is False

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

    async def test_first_entry_sets_resumed_flag(self) -> None:
        node = HumanInputNode("Approve?", next_node="target")
        node.name = "human_input"
        ctx = make_ctx()
        with pytest.raises(GraphInterrupt):
            await node.run(ctx)
        assert node._resumed is True

    async def test_interrupt_does_not_deliver(self) -> None:
        node = HumanInputNode("Approve?", next_node="target")
        node.name = "human_input"
        ctx = make_ctx()
        with pytest.raises(GraphInterrupt):
            await node.run(ctx)
        assert node._submit_result == {}


# ── Resume path: deliver ──────────────────────────────────────────────────


class TestHumanInputNodeResume:
    async def test_resume_delivers_signal(self) -> None:
        node = HumanInputNode("Approve?", next_node="downstream")
        node.name = "human_input"
        node._resumed = True
        ctx = make_ctx()
        await node.run(ctx)
        assert "downstream" in node._submit_result
        delivered = node._submit_result["downstream"]
        assert len(delivered) == 1
        assert delivered[0]["human_input"] == "resumed"
        assert delivered[0]["prompt"] == "Approve?"

    async def test_resume_resets_resumed_flag(self) -> None:
        node = HumanInputNode("Approve?", next_node="target")
        node.name = "human_input"
        node._resumed = True
        ctx = make_ctx()
        await node.run(ctx)
        assert node._resumed is False

    async def test_resume_returns_node_result(self) -> None:
        node = HumanInputNode("Approve?", next_node="target")
        node.name = "human_input"
        node._resumed = True
        ctx = make_ctx()
        result = await node.run(ctx)
        assert isinstance(result, NodeResult)

    async def test_resume_then_re_interrupt(self) -> None:
        node = HumanInputNode("Approve?", next_node="target")
        node.name = "human_input"
        node._resumed = True
        ctx = make_ctx()
        await node.run(ctx)
        assert node._resumed is False
        with pytest.raises(GraphInterrupt):
            await node.run(ctx)

    async def test_resume_deliver_to_end(self) -> None:
        from modex_graph import GraphNode

        node = HumanInputNode("Approve?", next_node=GraphNode.END)
        node.name = "human_input"
        node._resumed = True
        ctx = make_ctx()
        await node.run(ctx)
        assert GraphNode.END in node._submit_result


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

    def test_config_schema_returns_none(self) -> None:
        factory = HumanInputNodeFactory()
        assert factory.config_schema() is None

    def test_factory_is_node_factory_subclass(self) -> None:
        assert issubclass(HumanInputNodeFactory, NodeFactory)


# ── Factory rejects bad config ────────────────────────────────────────────


class TestHumanInputNodeFactoryRejectsBadConfig:
    def test_non_string_prompt_raises(self) -> None:
        factory = HumanInputNodeFactory()
        with pytest.raises(ValueError, match="prompt.*must be a string"):
            factory.create(NodeSpec(name="n1", node_type="human_input", config={"prompt": 123}))

    def test_list_prompt_raises(self) -> None:
        factory = HumanInputNodeFactory()
        with pytest.raises(ValueError, match="prompt.*must be a string"):
            factory.create(
                NodeSpec(name="n1", node_type="human_input", config={"prompt": ["a", "b"]})
            )

    def test_non_string_next_node_raises(self) -> None:
        factory = HumanInputNodeFactory()
        with pytest.raises(ValueError, match="next_node"):
            factory.create(
                NodeSpec(
                    name="n1",
                    node_type="human_input",
                    config={"prompt": "hi", "next_node": 123},
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

    async def test_registry_create_resume_then_deliver(self) -> None:
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
        node._resumed = True
        ctx = make_ctx()
        await node.run(ctx)
        assert "out" in node._submit_result
        assert node._submit_result["out"][0]["prompt"] == "Approve?"