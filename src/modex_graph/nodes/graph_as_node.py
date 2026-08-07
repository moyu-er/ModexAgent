# ruff: noqa: ANN401
"""`GraphAsNode` + `GraphAsNodeFactory` — a `CompiledGraph` wrapped as a Node.

Ticket 02 (P2.8): per ADR-0033 D8 (Graph-is-a-Node), a `CompiledGraph` is
already a `Node` subclass. This module provides a thin *wrapper* that:

- holds a `CompiledGraph` instance,
- awaits the inner graph's `execute(ctx)` on the shared `ctx`,
- delivers a completion signal to the next node via `self.deliver(...)`.

Design decision: a wrapper rather than modifying `CompiledGraph` directly.
The wrapper participates in the deliver/submit model while the compiled
graph remains responsible only for running its inner topology.

The inner graph shares `ctx.state` / `ctx.runtime` / `ctx.user_data` with
the parent (per D8). The subgraph writes its result to `ctx.state` (a
field on the state, per D9.3); the parent reads it after `execute` returns.
The wrapper delivers `{"subgraph_completed": True}` as a lightweight signal
— the parent reads the actual result from `ctx.state`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from ..compiled_graph import CompiledGraph
from ..integration import IntegratedInput
from ..node import Node
from ..node_factory import NodeFactory
from ..spec import GraphSpec, NodeSpec

if TYPE_CHECKING:
    from ..context import GraphContext
    from ..spec_compiler import GraphSpecCompiler


class GraphAsNodeConfig(BaseModel):
    """Pydantic config schema for `GraphAsNode` (rule 12 — strict-shape).

    Fields:
    - `graph_spec`: inline `GraphSpec` as a dict or a `GraphSpec` instance.
      Pydantic validates the shape; full `GraphSpec` validation +
      topology compilation happens in `create()`.
    - `next_node`: explicit deliver target for the completion signal (optional).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph_spec: dict[str, Any] | GraphSpec
    next_node: str | None = None


class GraphAsNode(Node[Any]):
    """Wraps a `CompiledGraph` as a `Node`, delivering the subgraph result.

    `execute()` runs the inner `CompiledGraph.execute(ctx)` on the shared
    `ctx`, then delivers a `{"subgraph_completed": True}` signal to the next
    node. The inner graph writes its actual result to `ctx.state` (per
    D9.3); the parent reads specific fields from there.
    """

    def __init__(
        self,
        compiled: CompiledGraph[Any],
        *,
        next_node: str | None = None,
    ) -> None:
        """Initialize the wrapper.

        Args:
            compiled: the inner `CompiledGraph` to run.
            next_node: the explicit deliver target for the completion signal.
                If `None`, the `_submit` step raises `NotImplementedError`
                (additive limitation).
        """
        self._compiled = compiled
        self._next_node = next_node

    async def execute(
        self,
        ctx: GraphContext[Any],
        integrated_input: IntegratedInput,
    ) -> None:
        """Run the inner graph and deliver a completion signal."""
        await self._compiled.execute(ctx, integrated_input)
        self.deliver({"subgraph_completed": True}, self._next_node, ctx)
        return None


class GraphAsNodeFactory(NodeFactory):
    """Creates `GraphAsNode` from an inline `GraphSpec` in config (ticket 02).

    `NodeSpec.config = {"graph_spec": <GraphSpec dict>, "next_node": <str>}`.

    Config shape is validated by `GraphAsNodeConfig` (returned from
    `config_schema()`). The `graph_spec` is then compiled via a
    `GraphSpecCompiler` (which requires a node registry and state-class mapping) —
    that is runtime validation, not config validation. The compiled graph is
    embedded in the `GraphAsNode` wrapper.
    """

    def __init__(self, compiler: GraphSpecCompiler) -> None:
        """Initialize with a `GraphSpecCompiler`.

        Args:
            compiler: the compiler used to materialize inline `GraphSpec`
                data into a `CompiledGraph`. The caller is responsible for
                wiring the compiler's node registry and state-class mapping.
        """
        self._compiler = compiler

    def create(self, spec: NodeSpec) -> Node[Any]:
        """Create a `GraphAsNode` from the spec's `graph_spec` config key.

        Config shape is validated via `GraphAsNodeConfig` — `graph_spec` is
        guaranteed to be a `dict` or `GraphSpec`, and `next_node` a `str |
        None`. A dict is then validated + compiled into a `CompiledGraph`
        via the compiler; a `GraphSpec` instance is compiled directly.

        Raises:
            pydantic.ValidationError: if `spec.config` fails config validation.
            pydantic.ValidationError: if a dict `graph_spec` fails
                `GraphSpec` validation.
            TopologyError: if topology validation fails during compilation.
        """
        config = GraphAsNodeConfig.model_validate(spec.config)
        if isinstance(config.graph_spec, dict):
            graph_spec = GraphSpec.model_validate(config.graph_spec)
        else:
            graph_spec = config.graph_spec
        compiled = self._compiler.compile(graph_spec)
        return GraphAsNode(compiled, next_node=config.next_node)

    def config_schema(self) -> type[BaseModel]:
        """Return `GraphAsNodeConfig` — the Pydantic config model."""
        return GraphAsNodeConfig


__all__ = ["GraphAsNode", "GraphAsNodeConfig", "GraphAsNodeFactory"]
