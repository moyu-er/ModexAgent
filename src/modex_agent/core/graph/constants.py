"""Core graph constants."""

from enum import StrEnum


class GraphNode(StrEnum):
    """Engine-recognized sentinel node names."""

    END = "__end__"
