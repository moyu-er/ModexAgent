"""Tests for ReAct graph constants."""
from framework.agents.react.constants import ReActNode, ReActReason, ReActMetaKey


class TestReActNode:
    def test_all_values_are_strings(self):
        for node in ReActNode:
            assert isinstance(node.value, str)

    def test_expected_nodes(self):
        values = {n.value for n in ReActNode}
        assert values >= {"start", "llm", "tool", "end"}


class TestReActReason:
    def test_all_values_are_strings(self):
        for reason in ReActReason:
            assert isinstance(reason.value, str)

    def test_expected_reasons(self):
        values = {r.value for r in ReActReason}
        expected = {
            "normal_start", "resume_tools", "has_tools", "no_tools",
            "max_iterations", "llm_error", "tools_done", "turn_cancelled", "done",
        }
        assert values == expected


class TestReActMetaKey:
    def test_all_keys_are_strings(self):
        keys = [
            getattr(ReActMetaKey, attr)
            for attr in dir(ReActMetaKey)
            if not attr.startswith("_")
        ]
        assert all(isinstance(k, str) for k in keys)

    def test_all_keys_start_with_underscore(self):
        """Meta keys should start with _ to indicate internal usage."""
        keys = [
            getattr(ReActMetaKey, attr)
            for attr in dir(ReActMetaKey)
            if not attr.startswith("_") and not callable(getattr(ReActMetaKey, attr, None))
        ]
        for k in keys:
            assert k.startswith("_"), f"Meta key {k!r} should start with '_'"
