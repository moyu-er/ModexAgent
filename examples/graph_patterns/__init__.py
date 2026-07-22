"""Reusable graph patterns built on `modex_graph`.

This package contains example graph topology patterns that compose the
public `modex_graph` API into higher-level workflows. It is example code
per ADR-0007 rule 9 — not framework code.
"""

from __future__ import annotations

from .conditional import (
    ConditionalNode,
    SwitchNode,
    build_conditional_graph,
)
from .map_reduce import (
    MapNode,
    ReduceNode,
    build_map_reduce_graph,
)
from .retry import (
    RetryNode,
    build_retry_graph,
)

__all__ = [
    # conditional.py (ticket 04)
    "ConditionalNode",
    "SwitchNode",
    "build_conditional_graph",
    # retry.py (ticket 05)
    "RetryNode",
    "build_retry_graph",
    # map_reduce.py (ticket 06)
    "MapNode",
    "ReduceNode",
    "build_map_reduce_graph",
]
