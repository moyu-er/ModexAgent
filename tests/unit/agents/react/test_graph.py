"""Tests for ReActGraph."""
from unittest.mock import MagicMock

from framework.agents.react.graph import ReActGraph
from framework.agents.react.constants import ReActNode, ReActReason


class TestReActGraph:
    def test_full_mode_has_all_nodes(self):
        g = ReActGraph(MagicMock(), mode="full")
        assert ReActNode.START in g._nodes
        assert ReActNode.LLM in g._nodes
        assert ReActNode.TOOL in g._nodes
        assert ReActNode.END in g._nodes

    def test_clean_mode_has_all_nodes(self):
        g = ReActGraph(MagicMock(), mode="clean")
        assert ReActNode.START in g._nodes
        assert ReActNode.LLM in g._nodes
        assert ReActNode.TOOL in g._nodes
        assert ReActNode.END in g._nodes

    def test_full_mode_all_edges_routable(self):
        g = ReActGraph(MagicMock(), mode="full")
        assert g.next_node(ReActNode.START, ReActReason.NORMAL_START) == ReActNode.LLM
        assert g.next_node(ReActNode.START, ReActReason.RESUME_TOOLS) == ReActNode.TOOL
        assert g.next_node(ReActNode.LLM, ReActReason.HAS_TOOLS) == ReActNode.TOOL
        assert g.next_node(ReActNode.LLM, ReActReason.NO_TOOLS) == ReActNode.END
        assert g.next_node(ReActNode.LLM, ReActReason.MAX_ITERATIONS) == ReActNode.END
        assert g.next_node(ReActNode.TOOL, ReActReason.TOOLS_DONE) == ReActNode.LLM
        assert g.next_node(ReActNode.TOOL, ReActReason.TURN_CANCELLED) == ReActNode.END

    def test_clean_mode_same_topology(self):
        g = ReActGraph(MagicMock(), mode="clean")
        assert g.next_node(ReActNode.LLM, ReActReason.HAS_TOOLS) == ReActNode.TOOL
        assert g.next_node(ReActNode.LLM, ReActReason.NO_TOOLS) == ReActNode.END
        assert g.next_node(ReActNode.TOOL, ReActReason.TOOLS_DONE) == ReActNode.LLM
