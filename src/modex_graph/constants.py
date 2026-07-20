"""Engine-recognized sentinel node names.

`GraphNode.START` and `GraphNode.END` are `StrEnum` sentinels that mark the
graph entry and terminal points. They are NOT real nodes — no `Node` instance
is registered under these names. Edges from `GraphNode.START` to a real node
declare the entry point; edges to `GraphNode.END` declare a terminal
transition.

Per ADR-0033 D9.2: business modules use `StrEnum` for their own node names
(e.g. `ReActNode.START/LLM/TOOL/END`); the engine's `GraphNode` is distinct
and reserved for the engine-level sentinels.
"""

from __future__ import annotations

from enum import StrEnum


class GraphNode(StrEnum):
    """Engine-recognized sentinel node names.

    These are sentinels, not real nodes. The graph entry is declared via
    `add_edge(GraphNode.START, real_entry_node)`. Terminal transitions target
    `GraphNode.END`.
    """

    START = "__start__"
    END = "__end__"
