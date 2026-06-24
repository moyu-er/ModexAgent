"""Tests for AgentResult dataclass with reasoning field.

验证 AgentResult 的 reasoning 字段功能，包括：
- reasoning 字段的创建和访问
- __repr__ 方法正确显示 reasoning 存在与否
- 与 content、error 等字段的兼容性
"""

from modex_agent.core.emitter import AgentResult


class TestAgentResult:
    """AgentResult dataclass tests."""

    def test_agent_result_basic(self):
        """Test basic AgentResult creation."""
        result = AgentResult(content="Hello")
        assert result.content == "Hello"
        assert result.reasoning is None
        assert result.stop_reason == "completed"
        assert result.error is None

    def test_agent_result_with_reasoning(self):
        """Test AgentResult with reasoning field."""
        result = AgentResult(
            content="The answer is 42",
            reasoning="Let me think... 20 + 22 = 42",
            stop_reason="completed",
        )
        assert result.content == "The answer is 42"
        assert result.reasoning == "Let me think... 20 + 22 = 42"

    def test_agent_result_reasoning_only(self):
        """Test AgentResult with only reasoning (edge case)."""
        result = AgentResult(
            content="",
            reasoning="Internal thought process",
        )
        assert result.content == ""
        assert result.reasoning == "Internal thought process"

    def test_agent_result_error_with_reasoning(self):
        """Test AgentResult with error and reasoning."""
        result = AgentResult(
            content="",
            reasoning="I was thinking but then...",
            error="Connection timeout",
            stop_reason="error",
        )
        assert result.error == "Connection timeout"
        assert result.reasoning == "I was thinking but then..."
        assert result.stop_reason == "error"

    def test_agent_result_repr_with_reasoning(self):
        """Test __repr__ includes reasoning indicator."""
        result = AgentResult(
            content="Answer",
            reasoning="Some reasoning",
        )
        repr_str = repr(result)
        assert "AgentResult" in repr_str
        assert "content='Answer'" in repr_str
        assert "reasoning=..." in repr_str  # Should indicate reasoning exists

    def test_agent_result_repr_without_reasoning(self):
        """Test __repr__ without reasoning."""
        result = AgentResult(content="Answer")
        repr_str = repr(result)
        assert "reasoning=None" in repr_str

    def test_agent_result_repr_with_error(self):
        """Test __repr__ with error."""
        result = AgentResult(error="Failed", stop_reason="error")
        repr_str = repr(result)
        assert "error='Failed'" in repr_str
        assert "content" not in repr_str  # Error case shouldn't show content

