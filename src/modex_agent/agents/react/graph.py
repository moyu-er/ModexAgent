"""``build_react_graph()`` — ReAct 6-node graph topology on ``modex_graph``.

Per ADR-0033 D9 + D13 Stage 4: replaces the old ``ReActGraph(Graph)`` class
(which subclassed the deleted ``modex_agent.core.graph.Graph``). The new
builder returns a ``modex_graph.Graph[ReActTurnState]`` ready for
``.compile(max_iterations=...)`` + ``GraphEngine``.

The topology uses 6 ReAct nodes and plain topology edges. The entry edge
``add_edge(GraphNode.START, ReActNode.START)``
declares ``StartNode`` as the graph entry. The terminal edge
``add_edge(ReActNode.END, GraphNode.END)`` routes ``EndNode`` to the engine
sentinel. Nodes route at runtime via ``deliver()`` — the former
``reason``-keyed edges were removed (P3.4b convergence).
"""

from __future__ import annotations

from typing import Literal

from modex_agent.agents.react.nodes.after_turn import AfterTurnNode
from modex_agent.agents.react.nodes.before_turn import BeforeTurnNode
from modex_graph.constants import GraphNode
from modex_graph.graph import Graph

from .constants import ReActNode
from .injection_drainer import InjectionDrainer
from .llm_client import ReactLlmClient
from .nodes.end import EndNode
from .nodes.llm import LLMNode
from .nodes.start import StartNode
from .nodes.tool import ToolNode
from .state import ReActTurnState
from .tool_dedup import ToolCallDeduplicator
from .tool_executor import ToolExecutor


def build_react_graph(
    *,
    llm_client: ReactLlmClient,
    injection_drainer: InjectionDrainer,
    tool_executor: ToolExecutor,
    mode: Literal["clean", "full"] = "full",
    deduplicator: ToolCallDeduplicator | None = None,
) -> Graph[ReActTurnState]:
    """Construct the ReAct 6-node graph topology on the new ``modex_graph`` engine.

    Returns a mutable ``Graph[ReActTurnState]`` — the caller is expected to
    ``.compile(max_iterations=...)`` it before constructing a ``GraphEngine``.
    Per ADR-0033 D9.3 the engine-level ``max_iterations`` is a panic safety
    net (larger than the business max); the business-level max is enforced
    by ``LLMNode`` delivering to ``ReActNode.AFTER``.

    Edges declare topology only — routing is deliver-only. Nodes call
    ``deliver(content, target, ctx)`` at runtime to route to the next node.
    Resume routing uses ``deliver(content, state.resume_target, ctx)`` from
    ``StartNode``.

    Topology::

        START → START_NODE → BEFORE → LLM ↔ TOOL → AFTER → END → GraphNode.END
                                  ↑                 │
                                  └─────────────────┘

    Two extra edges wire the engine sentinels: an entry edge
    ``GraphNode.START → ReActNode.START`` (declares the entry node) and a
    terminal edge ``ReActNode.END → GraphNode.END``.
    """
    g: Graph[ReActTurnState] = Graph(name=f"react_{mode}")

    # Nodes — registered under their ReActNode StrEnum names.
    g.add_node(ReActNode.START, StartNode())
    g.add_node(ReActNode.BEFORE, BeforeTurnNode())
    g.add_node(ReActNode.LLM, LLMNode(llm_client, injection_drainer))
    g.add_node(ReActNode.TOOL, ToolNode(tool_executor, deduplicator))
    g.add_node(ReActNode.AFTER, AfterTurnNode())
    g.add_node(ReActNode.END, EndNode())

    # Entry edge — declares StartNode as the graph entry. Exactly one edge
    # from GraphNode.START is required by Graph.compile().
    g.add_edge(GraphNode.START, ReActNode.START)

    # Topology edges — nodes route at runtime via deliver().
    g.add_edge(ReActNode.START, ReActNode.BEFORE)
    g.add_edge(ReActNode.START, ReActNode.TOOL)
    g.add_edge(ReActNode.BEFORE, ReActNode.LLM)
    g.add_edge(ReActNode.LLM, ReActNode.TOOL)
    g.add_edge(ReActNode.LLM, ReActNode.AFTER)
    g.add_edge(ReActNode.TOOL, ReActNode.LLM)
    g.add_edge(ReActNode.TOOL, ReActNode.AFTER)
    g.add_edge(ReActNode.AFTER, ReActNode.END)
    g.add_edge(ReActNode.AFTER, ReActNode.BEFORE)

    # Terminal edge from END to the engine sentinel.
    g.add_edge(ReActNode.END, GraphNode.END)

    return g


__all__ = ["build_react_graph"]
