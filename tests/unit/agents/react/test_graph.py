"""Tests for ``build_react_graph()`` — the ReAct 4-node graph topology on ``modex_graph``."""

from __future__ import annotations

from typing import Literal

from modex_graph.constants import GraphNode
from modex_graph.graph import Graph

from modex_agent.agents.react.constants import ReActNode, ReActReason
from modex_agent.agents.react.graph import build_react_graph
from modex_agent.agents.react.injection_drainer import InjectionDrainer
from modex_agent.agents.react.llm_client import ReactLlmClient
from modex_agent.agents.react.tool_executor import ToolExecutor


def _make_graph(mode: Literal["clean", "full"]) -> Graph:
    return build_react_graph(
        llm_client=ReactLlmClient(provider=object()),  # type: ignore[arg-type] — provider unused
        injection_drainer=InjectionDrainer(),
        tool_executor=ToolExecutor(default_tool_timeout=30.0),
        mode=mode,
    )


class TestBuildReActGraph:
    def test_returns_graph_instance(self) -> None:
        g = _make_graph("full")
        assert isinstance(g, Graph)

    def test_full_mode_name(self) -> None:
        g = _make_graph("full")
        assert g.name == "react_full"

    def test_clean_mode_name(self) -> None:
        g = _make_graph("clean")
        assert g.name == "react_clean"

    def test_has_all_four_nodes(self) -> None:
        g = _make_graph("full")
        assert ReActNode.START in g.nodes
        assert ReActNode.LLM in g.nodes
        assert ReActNode.TOOL in g.nodes
        assert ReActNode.END in g.nodes

    def test_compile_succeeds(self) -> None:
        g = _make_graph("full")
        compiled = g.compile(max_iterations=100)
        assert compiled.entry_node == ReActNode.START

    def test_all_static_edges_routable(self) -> None:
        g = _make_graph("full")
        compiled = g.compile(max_iterations=100)
        assert compiled.next_node_by_transition(ReActNode.START, ReActReason.NORMAL_START) == ReActNode.LLM
        assert compiled.next_node_by_transition(ReActNode.START, ReActReason.RESUME_TOOLS) == ReActNode.TOOL
        assert compiled.next_node_by_transition(ReActNode.LLM, ReActReason.HAS_TOOLS) == ReActNode.TOOL
        assert compiled.next_node_by_transition(ReActNode.LLM, ReActReason.NO_TOOLS) == ReActNode.END
        assert compiled.next_node_by_transition(ReActNode.LLM, ReActReason.MAX_ITERATIONS) == ReActNode.END
        assert compiled.next_node_by_transition(ReActNode.LLM, ReActReason.LLM_ERROR) == ReActNode.END
        assert compiled.next_node_by_transition(ReActNode.TOOL, ReActReason.TOOLS_DONE) == ReActNode.LLM
        assert compiled.next_node_by_transition(ReActNode.TOOL, ReActReason.TURN_CANCELLED) == ReActNode.END

    def test_end_node_has_default_edge_to_graph_end(self) -> None:
        # EndNode returns NodeResult(transition=None); the default edge
        # (reason=None) routes it to GraphNode.END.
        g = _make_graph("full")
        compiled = g.compile(max_iterations=100)
        assert compiled.default_edge_target(ReActNode.END) == GraphNode.END

    def test_entry_edge_declares_start_node(self) -> None:
        g = _make_graph("full")
        compiled = g.compile(max_iterations=100)
        assert compiled.entry_node == ReActNode.START

    def test_clean_mode_same_topology(self) -> None:
        g = _make_graph("clean")
        compiled = g.compile(max_iterations=100)
        assert compiled.next_node_by_transition(ReActNode.LLM, ReActReason.HAS_TOOLS) == ReActNode.TOOL
        assert compiled.next_node_by_transition(ReActNode.LLM, ReActReason.NO_TOOLS) == ReActNode.END
        assert compiled.next_node_by_transition(ReActNode.TOOL, ReActReason.TOOLS_DONE) == ReActNode.LLM
