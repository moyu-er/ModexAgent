"""Edge and Graph."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Generic

from typing_extensions import TypeVar

from .node import Node

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext

R = TypeVar("R", default=Any)


@dataclass(frozen=True)
class Edge:
    """Directed edge between nodes. reason=None means unconditional fallback."""

    source: str
    target: str
    reason: str | None = None


class Graph(Generic[R]):
    """A directed graph of named nodes and edges."""

    def __init__(self, name: str = "graph") -> None:
        self.name = name
        self._nodes: dict[str, Node[R]] = {}
        self._edges: dict[str, list[Edge]] = {}
        self.entry_node: str = "start"
        self.result_extractor: Callable[[AgentContext], R | None] | None = None

    def add_node(self, node: Node[R]) -> None:
        self._nodes[node.name] = node

    def get_node(self, name: str) -> Node[R]:
        """Return the node registered under ``name``. Raises KeyError if absent."""
        return self._nodes[name]

    def add_edge(self, source: str, target: str, reason: str | None = None) -> None:
        self._edges.setdefault(source, []).append(Edge(source, target, reason))

    def next_node(self, source: str, reason: str) -> str:
        """Match edge: exact reason first, then unconditional fallback."""
        candidates = self._edges.get(source, [])
        for edge in candidates:
            if edge.reason == reason:
                return edge.target
        for edge in candidates:
            if edge.reason is None:
                return edge.target
        raise KeyError(f"No edge from {source!r} for reason {reason!r}")
