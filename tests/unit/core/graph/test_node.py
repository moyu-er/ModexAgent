"""Tests for Node ABC and NodeTransition."""
import pytest
from framework.core.graph.node import Node, NodeTransition


class _TestNode(Node):
    def __init__(self, name: str, next_target: str = "__end__", next_reason: str = "done"):
        super().__init__(name)
        self._next = NodeTransition(next_target, next_reason)

    async def execute(self, ctx):
        return self._next


class TestNodeTransition:
    def test_create_transition(self):
        t = NodeTransition("target", "my_reason")
        assert t.target == "target"
        assert t.reason == "my_reason"

    def test_frozen(self):
        t = NodeTransition("a", "b")
        with pytest.raises(Exception):
            t.target = "c"


class TestNode:
    @pytest.mark.asyncio
    async def test_execute_returns_transition(self):
        node = _TestNode("test_node", "next_node", "some_reason")
        t = await node.execute({})
        assert t.target == "next_node"
        assert t.reason == "some_reason"

    def test_name_attribute(self):
        node = _TestNode("my_node")
        assert node.name == "my_node"
