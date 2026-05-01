"""Core graph constants."""
from enum import StrEnum


class GraphNode(StrEnum):
    """Engine-recognized sentinel node names."""
    END = "__end__"


class GraphMetaKey:
    """Keys used in ctx.metadata by the graph engine."""
    GRAPH_RESULT = "_graph_result"
