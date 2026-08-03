# ruff: noqa: ANN401
"""`GraphAsNode` + `GraphAsNodeFactory` — a `CompiledGraph` wrapped as a Node.

Ticket 02 (P2.8): per ADR-0033 D8 (Graph-is-a-Node), a `CompiledGraph` is
already a `Node` subclass. This module provides a thin *wrapper* that:

- holds a `CompiledGraph` instance,
- runs the inner graph's `execute(ctx)` on the shared `ctx`,
- delivers a completion signal to the next node via `self.deliver(...)`.

Design decision: a wrapper rather than modifying `CompiledGraph` directly.
`CompiledGraph` is a frozen dataclass whose `execute` returns an empty
`NodeResult` for the parent to route via its own edges. Adding deliver
semantics to `CompiledGraph.execute` would change the behaviour of every
existing subgraph call-site. The wrapper is additive and isolated — it
participates in the deliver/submit model without touching the existing
`CompiledGraph` contract.

The inner graph shares `ctx.state` / `ctx.runtime` / `ctx.user_data` with
the parent (per D8). The subgraph writes its result to `ctx.state` (a
field on the state, per D9.3); the parent reads it after `execute` returns.
The wrapper delivers `{"subgraph_completed": True}` as a lightweight signal
— the parent reads the actual result from `ctx.state`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ..compiled_graph import CompiledGraph
from ..integration import IntegratedInput
from ..node import Node
from ..node_factory import NodeFactory
from ..spec import GraphSpec, NodeSpec

if TYPE_CHECKING:
    from ..context import GraphContext
    from ..result import NodeResult
    from ..spec_compiler import GraphSpecCompiler


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
    ) -> NodeResult:
        """Run the inner graph and deliver a completion signal."""
        from ..result import NodeResult

        await self._compiled.execute(ctx, integrated_input)
        self.deliver({"subgraph_completed": True}, self._next_node, ctx)
        return NodeResult()


class GraphAsNodeFactory(NodeFactory):
    """Creates `GraphAsNode` from an inline `GraphSpec` in config (ticket 02).

    `NodeSpec.config = {"graph_spec": <GraphSpec dict>, "next_node": <str>}`.

    The factory compiles the `GraphSpec` via a `GraphSpecCompiler` (which
    requires `NodeRegistry` + `StateRegistry`). The compiled graph is
    embedded in the `GraphAsNode` wrapper.

    The factory does NOT declare a `config_schema` (returns `None`) — the
    `graph_spec` is validated by `GraphSpec.model_validate` + the compiler's
    topology validation at `create()` time.
    """

    def __init__(self, compiler: GraphSpecCompiler) -> None:
        """Initialize with a `GraphSpecCompiler`.

        Args:
            compiler: the compiler used to materialize inline `GraphSpec`
                data into a `CompiledGraph`. The caller is responsible for
                wiring the compiler's `NodeRegistry` + `StateRegistry`.
        """
        self._compiler = compiler

    def create(self, spec: NodeSpec) -> Node[Any]:
        """Create a `GraphAsNode` from the spec's `graph_spec` config key.

        Raises:
            ValueError: if `config["graph_spec"]` is missing.
            pydantic.ValidationError: if the `graph_spec` data fails
                `GraphSpec` validation.
            TopologyError: if topology validation fails during compilation.
        """
        graph_spec_data = spec.config.get("graph_spec")
        if graph_spec_data is None:
            raise ValueError(f"GraphAsNode requires a 'graph_spec' config key. Spec: {spec!r}.")
        if isinstance(graph_spec_data, dict):
            graph_spec = GraphSpec.model_validate(graph_spec_data)
        elif isinstance(graph_spec_data, GraphSpec):
            graph_spec = graph_spec_data
        else:
            raise ValueError(
                f"GraphAsNode 'graph_spec' must be a dict or GraphSpec "
                f"instance. Got: {type(graph_spec_data).__name__}."
            )
        compiled = self._compiler.compile(graph_spec)
        next_node = spec.config.get("next_node")
        if next_node is not None and not isinstance(next_node, str):
            raise ValueError(
                f"GraphAsNode 'next_node' config must be a string or None. Got: {next_node!r}."
            )
        return GraphAsNode(compiled, next_node=next_node)

    def config_schema(self) -> type[BaseModel] | None:
        """No Pydantic schema — config is validated in `create()`."""
        return None


__all__ = ["GraphAsNode", "GraphAsNodeFactory"]
