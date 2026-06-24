"""Tests for ReAct graph constants."""
from modex_agent.agents.react.constants import ReActNode, ReActReason


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
