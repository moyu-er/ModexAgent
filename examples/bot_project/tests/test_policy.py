"""Tests for ToolCallCleanupPolicy — pure logic, no framework dependencies."""

import pytest
from plugins.tool_call_cleanup.policy import ToolCallCleanupPolicy

_msg = lambda role, **kw: {"role": role, **kw}


class TestShouldCleanup:
    """验证 should_cleanup 判断逻辑。"""

    def test_empty_list_returns_false(self):
        policy = ToolCallCleanupPolicy()
        assert policy.should_cleanup([]) is False

    def test_last_is_assistant_without_tool_calls(self):
        policy = ToolCallCleanupPolicy()
        msgs = [
            _msg("user", content="hello"),
            _msg("assistant", content="hi there"),
        ]
        assert policy.should_cleanup(msgs) is True

    def test_last_is_assistant_with_tool_calls(self):
        policy = ToolCallCleanupPolicy()
        msgs = [
            _msg("user", content="run ls"),
            _msg("assistant", content="ok", tool_calls=[{"id": "t1", "type": "function", "function": {"name": "shell"}}]),
            _msg("tool", tool_call_id="t1", name="shell", content="file1"),
        ]
        assert policy.should_cleanup(msgs) is False

    def test_last_is_tool_message(self):
        policy = ToolCallCleanupPolicy()
        msgs = [
            _msg("user", content="run"),
            _msg("assistant", content="", tool_calls=[{"id": "t1", "type": "function", "function": {"name": "shell"}}]),
            _msg("tool", tool_call_id="t1", name="shell", content="output"),
        ]
        assert policy.should_cleanup(msgs) is False

    def test_last_is_user_message(self):
        policy = ToolCallCleanupPolicy()
        msgs = [_msg("user", content="hello")]
        assert policy.should_cleanup(msgs) is False


class TestClean:
    """验证 clean 行为：tool 消息和带 tool_calls 的 assistant 消息被移除。"""

    def test_removes_tool_and_intermediate_assistant(self):
        policy = ToolCallCleanupPolicy()
        msgs = [
            _msg("user", content="read a.py"),
            _msg("assistant", content="reading...", tool_calls=[{"id": "t1", "type": "function", "function": {"name": "read_file"}}]),
            _msg("tool", tool_call_id="t1", name="read_file", content="content here"),
            _msg("assistant", content="file content is: hello"),
        ]
        result = policy.clean(msgs)
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "read a.py"
        assert result[1]["role"] == "assistant"
        assert result[1]["content"] == "file content is: hello"

    def test_preserves_user_and_agent_messages(self):
        policy = ToolCallCleanupPolicy()
        msgs = [
            _msg("user", content="hi"),
            _msg("agent", content="msg from peer", source_agent="peer1"),
            _msg("assistant", content="got it"),
        ]
        result = policy.clean(msgs)
        assert len(result) == 3
        assert result[1]["role"] == "agent"

    def test_no_cleanup_when_turn_not_complete(self):
        policy = ToolCallCleanupPolicy()
        msgs = [
            _msg("user", content="run"),
            _msg("assistant", content="", tool_calls=[{"id": "t1", "type": "function", "function": {"name": "shell"}}]),
            _msg("tool", tool_call_id="t1", name="shell", content="output"),
        ]
        result = policy.clean(msgs)
        assert len(result) == 3  # unchanged

    def test_multi_tool_call_chain_cleaned(self):
        policy = ToolCallCleanupPolicy()
        msgs = [
            _msg("user", content="read and write"),
            _msg("assistant", content="", tool_calls=[
                {"id": "t1", "type": "function", "function": {"name": "read_file"}},
                {"id": "t2", "type": "function", "function": {"name": "write_file"}},
            ]),
            _msg("tool", tool_call_id="t1", name="read_file", content="old"),
            _msg("tool", tool_call_id="t2", name="write_file", content="ok"),
            _msg("assistant", content="done"),
        ]
        result = policy.clean(msgs)
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[1]["content"] == "done"

    def test_does_not_modify_input(self):
        policy = ToolCallCleanupPolicy()
        msgs = [
            _msg("user", content="hi"),
            _msg("assistant", content="hello"),
        ]
        original = list(msgs)
        policy.clean(msgs)
        assert msgs == original

    def test_handles_empty_tool_calls(self):
        """empty list should not match cleanup condition."""
        policy = ToolCallCleanupPolicy()
        msgs = [
            _msg("assistant", content="", tool_calls=[]),
        ]
        result = policy.clean(msgs)
        assert len(result) == 1


class TestIsSimulated:
    """验证 is_simulated 前缀识别。"""

    def test_detects_simulated_message(self):
        policy = ToolCallCleanupPolicy()
        msg = {"content": policy._SIMULATED_PREFIX + "some text"}
        assert policy.is_simulated(msg) is True

    def test_normal_message_not_simulated(self):
        policy = ToolCallCleanupPolicy()
        msg = {"content": "normal reply"}
        assert policy.is_simulated(msg) is False

    def test_empty_content_not_simulated(self):
        policy = ToolCallCleanupPolicy()
        msg = {"content": None}
        assert policy.is_simulated(msg) is False
