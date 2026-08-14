"""Executable graph END node and its default factory."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from ..constants import NodeTrigger
from ..integration import GraphPayload, IntegratedInput
from ..node import Node
from ..node_factory import NodeFactory
from ..spec import NodeSpec
from ..state.default_state import DefaultGraphState

if TYPE_CHECKING:
    from ..context import GraphContext
    from ..state import GraphState


class _EndNodeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EndNode[S: "GraphState"](Node[S]):
    """Default graph terminal that aggregates delivered content into static state."""

    # Forced: END must always wait for all predecessors, even when
    # default_trigger=ON_RECEIVE, or parallel delivers overwrite state.result.
    trigger: NodeTrigger | None = NodeTrigger.ON_ALL_PREDS

    async def execute(
        self,
        ctx: GraphContext[S],
        integrated_input: IntegratedInput,
    ) -> None:
        if isinstance(ctx.state, DefaultGraphState):
            ctx.state.result = [
                payload.content
                if isinstance(payload.content, GraphPayload)
                else GraphPayload(content=str(payload.content))
                for payload in integrated_input.payloads
            ]


class DefaultEndNodeFactory(NodeFactory):
    """Create the framework-provided END node."""

    def create(self, spec: NodeSpec) -> Node[Any]:
        return EndNode()

    def config_schema(self) -> type[BaseModel]:
        return _EndNodeConfig


__all__ = ["DefaultEndNodeFactory", "EndNode"]
