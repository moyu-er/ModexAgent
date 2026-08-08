"""Graph-aware delivery targets and the agent-facing deliver tool."""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from modex_agent.agents.agent_node import AgentNode
from modex_agent.core.agent import current_agent_context
from modex_agent.core.tool_manager import Tool, ToolConfig
from modex_graph.compiled_graph import CompiledGraph
from modex_graph.constants import GraphNode
from modex_graph.integration import GraphPayload


class GraphDeliverTarget(BaseModel):
    """Downstream graph node exposed as an agent delivery target."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str


class GraphDeliverTargetStore:
    """Derive available delivery targets from a compiled graph topology."""

    def __init__(self, graph_ref: CompiledGraph[Any], current_node: str) -> None:
        self._graph = graph_ref
        self._current = current_node

    def list(self) -> list[GraphDeliverTarget]:
        """Return downstream nodes in edge declaration order, excluding END."""
        targets: list[GraphDeliverTarget] = []
        for edge in self._graph.edges_from(self._current):
            if edge.target == GraphNode.END:
                continue
            node = self._graph.nodes[edge.target]
            description = (
                node.resolve_description()
                if isinstance(node, AgentNode)
                else f"Graph node {edge.target!r}"
            )
            targets.append(
                GraphDeliverTarget(name=edge.target, description=description)
            )
        return targets

    def get(self, name: str) -> GraphDeliverTarget | None:
        """Return the downstream target with ``name``, if available."""
        return next((target for target in self.list() if target.name == name), None)

    def resolve_node_id(self, name: str) -> str:
        """Resolve a graph node name to its persistent node ID for display."""
        return self._graph.nodes[name].node_id

_DELIVER_PARAMETERS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "target": {
            "type": "string",
            "description": (
                "Optional exact downstream node name. Omit to deliver to all "
                "downstream nodes."
            ),
        },
        "content": {
            "type": "string",
            "description": "Self-contained content for the downstream node.",
        },
    },
    "required": ["content"],
}


class GraphDeliverTool(Tool):
    """Deliver agent content to one named node or all downstream nodes."""

    def __init__(self, node: AgentNode, store: GraphDeliverTargetStore) -> None:
        self._node = node
        self._store = store
        super().__init__(
            name="deliver",
            parameters=_DELIVER_PARAMETERS,
            config=ToolConfig(),
        )

    @property
    def description(self) -> str:
        targets = self._store.list()
        available = (
            ", ".join(
                f"{target.name} (ID: {self._store.resolve_node_id(target.name)}; "
                f"{target.description})"
                for target in targets
            )
            if targets
            else "none"
        )
        return (
            "Deliver content to a downstream node. "
            f"Available targets: {available}. "
            "If no target is specified, content is delivered to all downstream "
            "nodes (auto-deliver)."
        )

    def get_dynamic_schema(self) -> dict[str, Any]:
        """Return a schema whose target enum matches the current topology."""
        schema = super().get_dynamic_schema()
        function = dict(schema.get("function", {}))
        parameters = dict(function.get("parameters", {}))
        properties = dict(parameters.get("properties", {}))
        target = dict(properties.get("target", {}))
        target["enum"] = [item.name for item in self._store.list()]
        properties["target"] = target
        parameters["properties"] = properties
        function["parameters"] = parameters
        return {**schema, "function": function}

    async def execute(self, **kwargs: Any) -> str:  # noqa: ANN401 - Tool ABC contract
        """Accumulate a typed graph delivery using the target's node name."""
        target_value = kwargs.get("target")
        target_name = None if target_value is None else str(target_value)
        payload = GraphPayload(content=str(kwargs.get("content", "")))

        agent_context = current_agent_context.get(None)
        graph_context = agent_context.graph_context if agent_context is not None else None
        if graph_context is None:
            return "Error: deliver tool called outside graph context."

        if target_name is None:
            self._node.deliver(payload, None, graph_context)
            return "Delivered to all downstream nodes."

        if self._store.get(target_name) is None:
            available = ", ".join(target.name for target in self._store.list())
            return (
                f"Error: {target_name!r} is not a valid downstream node. "
                f"Available: {available}"
            )

        self._node.deliver(payload, target_name, graph_context)
        return f"Delivered to {target_name!r}."


__all__ = ["GraphDeliverTarget", "GraphDeliverTargetStore", "GraphDeliverTool"]
