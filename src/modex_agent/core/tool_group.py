from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from modex_agent.core.tool_manager import Tool


class ToolGroupVariant(BaseModel):
    """One legal runtime variant and its exact ordered member names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    tools: tuple[str, ...]


class ToolGroupSpec(BaseModel):
    """Compile-time manifest for a group selected through one tool anchor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    anchor: str
    variants: tuple[ToolGroupVariant, ...]


class ToolGroupResource(ABC):
    """Async resource adopted into an agent's ordered lifecycle.

    Tool groups are the primary producers; assembly prerequisites such as a
    sandbox guard use the same contract so one owner can preserve dependency
    order from construction through teardown.
    """

    @abstractmethod
    async def aclose(self) -> None:
        """Close the runtime resource. Implementations must be idempotent."""


class ToolGroup:
    """Runtime TOOL-factory product containing an atomic set of tools."""

    __slots__ = ("_anchor", "_resource", "_tools", "_variant")

    def __init__(
        self,
        *,
        anchor: str,
        variant: str,
        tools: tuple[Tool, ...],
        resource: ToolGroupResource | None = None,
    ) -> None:
        self._anchor = anchor
        self._variant = variant
        self._tools = tools
        self._resource = resource

    @property
    def anchor(self) -> str:
        return self._anchor

    @property
    def variant(self) -> str:
        return self._variant

    @property
    def tools(self) -> tuple[Tool, ...]:
        return self._tools

    @property
    def resource(self) -> ToolGroupResource | None:
        return self._resource


__all__ = ["ToolGroup", "ToolGroupResource", "ToolGroupSpec", "ToolGroupVariant"]
