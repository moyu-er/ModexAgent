"""Default mutable state for static graph execution."""

from __future__ import annotations

from pydantic import ConfigDict

from ..integration import GraphPayload
from .state import GraphState


class DefaultGraphState(GraphState):
    """Static graph state whose END node records aggregated payloads."""

    model_config = ConfigDict(frozen=False, extra="forbid")

    result: list[GraphPayload] | None = None


__all__ = ["DefaultGraphState"]
