"""Executable graph START node and its default factory."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from ..integration import IntegratedInput
from ..node import Node
from ..node_factory import NodeFactory
from ..spec import NodeSpec

if TYPE_CHECKING:
    from ..context import GraphContext
    from ..state import GraphState


class _StartNodeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StartNode[S: "GraphState"](Node[S]):
    """Default graph entry that activates every declared downstream edge."""

    async def execute(
        self,
        ctx: GraphContext[S],
        integrated_input: IntegratedInput,
    ) -> None:
        self.deliver(ctx.user_input, None, ctx)


class DefaultStartNodeFactory(NodeFactory):
    """Create the framework-provided START node."""

    def create(self, spec: NodeSpec) -> Node[Any]:
        return StartNode()

    def config_schema(self) -> type[BaseModel]:
        return _StartNodeConfig


__all__ = ["DefaultStartNodeFactory", "StartNode"]
