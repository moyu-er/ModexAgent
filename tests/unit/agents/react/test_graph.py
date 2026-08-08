"""Tests for ``build_react_graph()`` — the ReAct 4-node graph topology on ``modex_graph``."""

from __future__ import annotations

import re
from typing import Literal

from modex_graph.constants import GraphNode
from modex_graph.graph import Graph

from modex_agent.agents.react.constants import ReActNode
from modex_agent.agents.react.graph import build_react_graph
from modex_agent.agents.react.injection_drainer import InjectionDrainer
from modex_agent.agents.react.llm_client import ReactLlmClient
from modex_agent.agents.react.tool_executor import ToolExecutor


def _make_graph(mode: Literal["clean", "full"]) -> Graph:
    return build_react_graph(
        llm_client=ReactLlmClient(provider=object()),  # type: ignore[arg-type] — provider unused
        injection_drainer=InjectionDrainer(),
        tool_executor=ToolExecutor(),
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

    def test_all_nodes_receive_unique_node_ids(self) -> None:
        g = _make_graph("full")

        node_ids = [node.node_id for node in g.nodes.values()]

        assert all(re.fullmatch(r"node_[0-9a-f]{12}[0-9A-Za-z]{14}", value) for value in node_ids)
        assert len(set(node_ids)) == len(node_ids)

    def test_compile_succeeds(self) -> None:
        g = _make_graph("full")
        compiled = g.compile(max_iterations=100)
        assert compiled.entry_node == GraphNode.START

    def test_all_topology_edges_present(self) -> None:
        g = _make_graph("full")
        compiled = g.compile(max_iterations=100)
        start_targets = {e.target for e in compiled.edges_from(ReActNode.START)}
        assert ReActNode.LLM in start_targets

        llm_targets = {e.target for e in compiled.edges_from(ReActNode.LLM)}
        assert ReActNode.TOOL in llm_targets
        assert ReActNode.END in llm_targets

        tool_targets = {e.target for e in compiled.edges_from(ReActNode.TOOL)}
        assert ReActNode.LLM in tool_targets
        assert ReActNode.END in tool_targets

    def test_end_node_has_edge_to_graph_end(self) -> None:
        g = _make_graph("full")
        compiled = g.compile(max_iterations=100)
        end_targets = {e.target for e in compiled.edges_from(ReActNode.END)}
        assert GraphNode.END in end_targets

    def test_entry_edge_declares_start_node(self) -> None:
        g = _make_graph("full")
        compiled = g.compile(max_iterations=100)
        assert compiled.entry_node == GraphNode.START

    def test_clean_mode_same_topology(self) -> None:
        g = _make_graph("clean")
        compiled = g.compile(max_iterations=100)
        llm_targets = {e.target for e in compiled.edges_from(ReActNode.LLM)}
        assert ReActNode.TOOL in llm_targets
        assert ReActNode.END in llm_targets
        tool_targets = {e.target for e in compiled.edges_from(ReActNode.TOOL)}
        assert ReActNode.LLM in tool_targets
