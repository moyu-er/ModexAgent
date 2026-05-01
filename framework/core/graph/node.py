"""Node ABC and NodeTransition."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from framework.core.agent import AgentContext


@dataclass(frozen=True)
class NodeTransition:
    """A node's routing instruction: which node to go to next, and why."""
    target: str
    reason: str


class Node(ABC):
    """Abstract graph node. Executes logic and returns a routing transition."""

    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    async def execute(self, ctx: AgentContext) -> NodeTransition:
        """Execute node logic and return the next node transition."""
        ...
