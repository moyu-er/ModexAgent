"""Tests for interrupt(), GraphInterrupt."""
import pytest
from modex_agent.core.graph.interrupt import GraphInterrupt, interrupt


class TestGraphInterrupt:
    def test_create(self):
        exc = GraphInterrupt(["req1"], node_name="tool", iteration=3)
        assert exc.value == ["req1"]
        assert exc.node_name == "tool"
        assert exc.iteration == 3

    def test_create_defaults(self):
        exc = GraphInterrupt(["req1"])
        assert exc.value == ["req1"]
        assert exc.node_name == ""
        assert exc.iteration == 0

    def test_is_exception(self):
        exc = GraphInterrupt(None)
        assert isinstance(exc, Exception)


class TestInterruptFunction:
    def test_always_raises_graphinterrupt(self):
        with pytest.raises(GraphInterrupt) as exc_info:
            interrupt(["req_a", "req_b"])
        assert exc_info.value.value == ["req_a", "req_b"]
