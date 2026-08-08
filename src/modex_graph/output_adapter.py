"""Graph execution output values and adapter contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class GraphOutputKind(StrEnum):
    """Terminal graph execution outcomes."""

    COMPLETED = "graph_completed"
    CRASHED = "graph_crashed"


class GraphOutput(BaseModel):
    """Terminal output emitted for a graph instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: GraphOutputKind
    graph_instance_id: int
    result: Any = None
    error: str | None = None


class GraphOutputAdapter(ABC):
    """Receives terminal graph execution outputs."""

    @abstractmethod
    async def emit(self, output: GraphOutput) -> None:
        """Emit a terminal graph output."""


__all__ = ["GraphOutput", "GraphOutputAdapter", "GraphOutputKind"]
