"""Tests for ReActGraph."""
from modex_agent.agents.react.constants import ReActNode, ReActReason
from modex_agent.agents.react.graph import ReActGraph
from modex_agent.agents.react.injection_drainer import InjectionDrainer
from modex_agent.agents.react.llm_client import ReactLlmClient
from modex_agent.agents.react.tool_executor import ToolExecutor


def _make_graph(mode: str) -> ReActGraph:
    return ReActGraph(
        llm_client=ReactLlmClient(provider=object()),
        injection_drainer=InjectionDrainer(),
        tool_executor=ToolExecutor(default_tool_timeout=30.0),
        mode=mode,
    )


class TestReActGraph:
    def test_full_mode_has_all_nodes(self):
        g = _make_graph("full")
        assert ReActNode.START in g._nodes
        assert ReActNode.LLM in g._nodes
        assert ReActNode.TOOL in g._nodes
        assert ReActNode.END in g._nodes

    def test_clean_mode_has_all_nodes(self):
        g = _make_graph("clean")
        assert ReActNode.START in g._nodes
        assert ReActNode.LLM in g._nodes
        assert ReActNode.TOOL in g._nodes
        assert ReActNode.END in g._nodes

    def test_full_mode_all_edges_routable(self):
        g = _make_graph("full")
        assert g.next_node(ReActNode.START, ReActReason.NORMAL_START) == ReActNode.LLM
        assert g.next_node(ReActNode.START, ReActReason.RESUME_TOOLS) == ReActNode.TOOL
        assert g.next_node(ReActNode.LLM, ReActReason.HAS_TOOLS) == ReActNode.TOOL
        assert g.next_node(ReActNode.LLM, ReActReason.NO_TOOLS) == ReActNode.END
        assert g.next_node(ReActNode.LLM, ReActReason.MAX_ITERATIONS) == ReActNode.END
        assert g.next_node(ReActNode.TOOL, ReActReason.TOOLS_DONE) == ReActNode.LLM
        assert g.next_node(ReActNode.TOOL, ReActReason.TURN_CANCELLED) == ReActNode.END

    def test_clean_mode_same_topology(self):
        g = _make_graph("clean")
        assert g.next_node(ReActNode.LLM, ReActReason.HAS_TOOLS) == ReActNode.TOOL
        assert g.next_node(ReActNode.LLM, ReActReason.NO_TOOLS) == ReActNode.END
        assert g.next_node(ReActNode.TOOL, ReActReason.TOOLS_DONE) == ReActNode.LLM
