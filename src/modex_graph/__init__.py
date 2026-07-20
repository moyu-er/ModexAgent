"""modex_graph — generalized typed graph engine.

Framework-agnostic sibling of `modex_agent`. Depends only on
`pydantic` + the Python standard library. The reverse dependency
(`modex_agent` → `modex_graph`) is required; `modex_graph` → `modex_agent`
is FORBIDDEN and enforced by `tests/architecture/test_modex_graph_isolation.py`.

Public surface (ADR-0033 acceptance criteria):

- **Graph primitives:** `Graph`, `Node`, `CompiledGraph`, `GraphEngine`,
  `GraphNode` (START/END sentinels).
- **Context + runtime:** `GraphContext`, `GraphRuntime`.
- **State + channels:** `GraphState`, `BaseChannel`, `LastValue`,
  `ReducerChannel`, `Codec`, `register_codec`.
- **Result types:** `NodeResult`, `Command`, `Task`.
- **Exceptions:** `GraphBubbleUp`, `GraphInterrupt`, `GraphDrained`,
  `ParentCommand`, `RoutingError`, `GraphRecursionError`.

See `docs/adr/0033-generalized-graph-engine.md` for the authoritative design.
"""

from __future__ import annotations

from .channel import (
    BaseChannel,
    Codec,
    JsonValue,
    LastValue,
    ReducerChannel,
    register_codec,
)
from .compiled_graph import CompiledGraph
from .constants import GraphNode
from .context import GraphContext
from .engine import GraphEngine
from .exceptions import (
    GraphBubbleUp,
    GraphDrained,
    GraphInterrupt,
    GraphRecursionError,
    ParentCommand,
    RoutingError,
)
from .graph import ConditionalEdge, Edge, Graph
from .node import Node
from .result import Command, NodeResult, Task
from .runtime import GraphRuntime
from .state import GraphState

__all__ = [
    # Graph primitives
    "Graph",
    "Node",
    "CompiledGraph",
    "GraphEngine",
    "GraphNode",
    "Edge",
    "ConditionalEdge",
    # Context + runtime
    "GraphContext",
    "GraphRuntime",
    # State + channels
    "GraphState",
    "BaseChannel",
    "LastValue",
    "ReducerChannel",
    "Codec",
    "register_codec",
    "JsonValue",
    # Result types
    "NodeResult",
    "Command",
    "Task",
    # Exceptions
    "GraphBubbleUp",
    "GraphInterrupt",
    "GraphDrained",
    "ParentCommand",
    "RoutingError",
    "GraphRecursionError",
]

__version__ = "1.0.0"
