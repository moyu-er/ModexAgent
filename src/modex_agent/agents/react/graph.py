"""ReActGraph — clean or full ReAct loop as a Graph."""

from __future__ import annotations

from typing import Any, Literal

from modex_agent.agents.react.constants import ReActNode, ReActReason
from modex_agent.agents.react.injection_drainer import InjectionDrainer
from modex_agent.agents.react.llm_client import ReactLlmClient
from modex_agent.agents.react.nodes.end import EndNode
from modex_agent.agents.react.nodes.llm import LLMNode
from modex_agent.agents.react.nodes.start import StartNode
from modex_agent.agents.react.nodes.tool import ToolNode
from modex_agent.agents.react.tool_executor import ToolExecutor
from modex_agent.core.agent import AgentContext
from modex_agent.core.graph.graph import Graph
from modex_agent.runtime.enums import TurnCustomKey


class ReActGraph(Graph):
    def __init__(
        self,
        *,
        llm_client: ReactLlmClient,
        injection_drainer: InjectionDrainer,
        tool_executor: ToolExecutor,
        mode: Literal["clean", "full"] = "full",
    ) -> None:
        super().__init__(name=f"react_{mode}")
        self.result_extractor = self._extract_graph_result

        self.add_node(StartNode())
        self.add_node(LLMNode(llm_client, injection_drainer))
        self.add_node(ToolNode(tool_executor))
        self.add_node(EndNode())

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

    def _extract_graph_result(self, ctx: AgentContext) -> Any:
        """Read the graph result the EndNode stored in turn state."""
        return ctx.runtime.state.custom.get(TurnCustomKey.GRAPH_RESULT) if ctx.runtime else None
