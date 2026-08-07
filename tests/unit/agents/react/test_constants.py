"""Tests for ReAct graph constants."""
from modex_agent.agents.react.constants import ReActNode


class TestReActNode:
    def test_all_values_are_strings(self):
        for node in ReActNode:
            assert isinstance(node.value, str)

    def test_expected_nodes(self):
        values = {n.value for n in ReActNode}
        assert values >= {"start", "llm", "tool", "end"}
