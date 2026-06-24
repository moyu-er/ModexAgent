"""ReActGraph — clean or full ReAct loop as a Graph."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from modex_agent.agents.react.constants import ReActNode, ReActReason
from modex_agent.agents.react.nodes.end import EndNode
from modex_agent.agents.react.nodes.llm import LLMNode
from modex_agent.agents.react.nodes.start import StartNode
from modex_agent.agents.react.nodes.tool import ToolNode
from modex_agent.core.graph.graph import Graph

if TYPE_CHECKING:
    from modex_agent.agents.react.agent import ReActAgent


class ReActGraph(Graph):
    def __init__(self, agent: ReActAgent, *, mode: Literal["clean", "full"] = "full") -> None:
        super().__init__(name=f"react_{mode}")

        self.add_node(StartNode())
        self.add_node(LLMNode(agent))
        self.add_node(ToolNode(agent))
        self.add_node(EndNode(agent))

        # start edges
        self.add_edge(ReActNode.START, ReActNode.LLM, reason=ReActReason.NORMAL_START)
        self.add_edge(ReActNode.START, ReActNode.TOOL, reason=ReActReason.RESUME_TOOLS)

        # llm edges
        self.add_edge(ReActNode.LLM, ReActNode.TOOL, reason=ReActReason.HAS_TOOLS)
        self.add_edge(ReActNode.LLM, ReActNode.END, reason=ReActReason.NO_TOOLS)
        self.add_edge(ReActNode.LLM, ReActNode.END, reason=ReActReason.MAX_ITERATIONS)
        self.add_edge(ReActNode.LLM, ReActNode.END, reason=ReActReason.LLM_ERROR)

        # tool edges
        self.add_edge(ReActNode.TOOL, ReActNode.LLM, reason=ReActReason.TOOLS_DONE)
        self.add_edge(ReActNode.TOOL, ReActNode.END, reason=ReActReason.TURN_CANCELLED)
