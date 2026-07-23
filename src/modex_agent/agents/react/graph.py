"""``build_react_graph()`` — ReAct 4-node graph topology on ``modex_graph``.

Per ADR-0033 D9 + D13 Stage 4: replaces the old ``ReActGraph(Graph)`` class
(which subclassed the deleted ``modex_agent.core.graph.Graph``). The new
builder returns a ``modex_graph.Graph[ReActTurnState]`` ready for
``.compile(max_iterations=...)`` + ``GraphEngine``.

The topology is identical to the old ReActGraph — 4 nodes
(``StartNode`` / ``LLMNode`` / ``ToolNode`` / ``EndNode``) and 8 static edges
keyed by ``ReActReason`` values. The entry edge
``add_edge(GraphNode.START, ReActNode.START)`` declares ``StartNode`` as the
graph entry. The terminal edge ``add_edge(ReActNode.END, GraphNode.END)``
is a default edge (``reason=None``) so ``EndNode`` returns
``NodeResult(transition=None)`` and the engine routes to ``GraphNode.END``
via the default-edge fallback (D6 priority 5).
"""

from __future__ import annotations

from typing import Literal

from modex_graph.constants import GraphNode
from modex_graph.graph import Graph

from .constants import ReActNode, ReActReason
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
    """Construct the ReAct 4-node graph topology on the new ``modex_graph`` engine.

    Returns a mutable ``Graph[ReActTurnState]`` — the caller is expected to
    ``.compile(max_iterations=...)`` it before constructing a ``GraphEngine``.
    Per ADR-0033 D9.3 the engine-level ``max_iterations`` is a panic safety
    net (larger than the business max); the business-level max is enforced
    by ``LLMNode`` returning ``transition=ReActReason.MAX_ITERATIONS``.

    The 7 static edges preserve the existing ReAct topology (resume routing
    is now via `Command(goto=...)` from `state.resume_target`, not a static
    edge):

    ```
    START --NORMAL_START--> LLM
    LLM   --HAS_TOOLS--> TOOL
    LLM   --NO_TOOLS--> END
    LLM   --MAX_ITERATIONS--> END
    LLM   --LLM_ERROR--> END
    TOOL  --TOOLS_DONE--> LLM
    TOOL  --TURN_CANCELLED--> END
    ```

    Two extra edges wire the engine sentinels: an entry edge
    ``GraphNode.START → ReActNode.START`` (declares the entry node) and a
    default edge ``ReActNode.END → GraphNode.END`` (so ``EndNode``'s
    ``NodeResult(transition=None)`` routes to the engine's END sentinel
    via the default-edge fallback).
    """
    g: Graph[ReActTurnState] = Graph(name=f"react_{mode}")

    # Nodes — registered under their ReActNode StrEnum names.
    g.add_node(ReActNode.START, StartNode())
    g.add_node(ReActNode.LLM, LLMNode(llm_client, injection_drainer))
    g.add_node(ReActNode.TOOL, ToolNode(tool_executor, deduplicator))
    g.add_node(ReActNode.END, EndNode())

    # Entry edge — declares StartNode as the graph entry. Exactly one edge
    # from GraphNode.START is required by Graph.compile().
    g.add_edge(GraphNode.START, ReActNode.START)

    # Static edges — keyed by ReActReason values (StrEnum satisfies str).
    # Resume routing uses Command(goto=...) from state.resume_target
    # (priority-1 dynamic routing), not a static edge.
    g.add_edge(ReActNode.START, ReActNode.LLM, reason=ReActReason.NORMAL_START)
    g.add_edge(ReActNode.LLM, ReActNode.TOOL, reason=ReActReason.HAS_TOOLS)
    g.add_edge(ReActNode.LLM, ReActNode.END, reason=ReActReason.NO_TOOLS)
    g.add_edge(ReActNode.LLM, ReActNode.END, reason=ReActReason.MAX_ITERATIONS)
    g.add_edge(ReActNode.LLM, ReActNode.END, reason=ReActReason.LLM_ERROR)
    g.add_edge(ReActNode.TOOL, ReActNode.LLM, reason=ReActReason.TOOLS_DONE)
    g.add_edge(ReActNode.TOOL, ReActNode.END, reason=ReActReason.TURN_CANCELLED)

    # Default edge from END to the engine sentinel. EndNode returns
    # NodeResult(transition=None); the engine falls through to this default
    # edge and routes to GraphNode.END, terminating the loop.
    g.add_edge(ReActNode.END, GraphNode.END)

    return g


__all__ = ["build_react_graph"]
