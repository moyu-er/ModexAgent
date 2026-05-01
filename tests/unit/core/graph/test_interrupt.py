"""Tests for interrupt(), GraphInterrupt."""
import pytest
from framework.core.graph.interrupt import (
    GraphInterrupt, interrupt, _current_resume,
)


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
    def test_raises_when_no_resume(self):
        token = _current_resume.set(None)
        try:
            with pytest.raises(GraphInterrupt) as exc_info:
                interrupt(["req_a", "req_b"])
            assert exc_info.value.value == ["req_a", "req_b"]
        finally:
            _current_resume.reset(token)

    def test_returns_resume_value_when_set(self):
        token = _current_resume.set(["ALLOWED", "DENIED"])
        try:
            result = interrupt(["req_a", "req_b"])
            assert result == ["ALLOWED", "DENIED"]
        finally:
            _current_resume.reset(token)
