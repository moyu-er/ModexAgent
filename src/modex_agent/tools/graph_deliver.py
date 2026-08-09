"""Graph-aware delivery targets and the agent-facing deliver tool."""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from modex_agent.agents.agent_node import AgentNode
from modex_agent.core.agent import current_agent_context
from modex_agent.core.tool_manager import Tool, ToolConfig
from modex_agent.runtime.enums import TurnCustomKey
from modex_graph.compiled_graph import CompiledGraph
from modex_graph.constants import GraphNode
from modex_graph.integration import GraphPayload


class GraphDeliverTarget(BaseModel):
    """Downstream graph node exposed as an agent delivery target."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str


class DeliverResult(BaseModel):
    """Outcome of a deliver attempt — shared across tool, REST, and CLI.

    All three deliver paths (agent tool, REST route, modexctl CLI) produce
    the same ``message`` string for the same outcome, so logs and user-facing
    feedback are identical regardless of entry point.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    target: str | None = None
    message: str = ""

    @classmethod
    def missing_target(cls, available: list[str]) -> DeliverResult:
        names = ", ".join(available) if available else "none"
        return cls(
            ok=False,
            target=None,
            message=f"Error: target is required. Available: {names}. Specify one target.",
        )

    @classmethod
    def invalid_target(cls, target_name: str, available: list[str]) -> DeliverResult:
        names = ", ".join(available) if available else "none"
        return cls(
            ok=False,
            target=target_name,
            message=f"Error: {target_name!r} is not a valid downstream node. Available: {names}.",
        )

    @classmethod
    def success(cls, target_name: str) -> DeliverResult:
        return cls(
            ok=True,
            target=target_name,
            message=f"Delivered to {target_name!r}.",
        )


class GraphDeliverTargetStore:
    """Derive available delivery targets from a compiled graph topology."""

    def __init__(self, graph_ref: CompiledGraph[Any], current_node: str) -> None:
        self._graph = graph_ref
        self._current = current_node

    def list(self) -> list[GraphDeliverTarget]:
        """Return downstream targets in edge declaration order, including END."""
        targets: list[GraphDeliverTarget] = []
        for edge in self._graph.edges_from(self._current):
            if edge.target == GraphNode.END:
                targets.append(
                    GraphDeliverTarget(
                        name=GraphNode.END,
                        description=(
                            "Workflow terminal. Deliver here ONLY when your "
                            "task is fully complete and no downstream node "
                            "needs to process your output further. Do not "
                            "deliver to END and another target in the same "
                            "turn — choose one: route to a downstream node "
                            "for further processing, or to END to finish."
                        ),
                    )
                )
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

    def names(self) -> list[str]:
        """Return just the target names — for error messages."""
        return [t.name for t in self.list()]

    def get(self, name: str) -> GraphDeliverTarget | None:
        """Return the downstream target with ``name``, if available."""
        return next((target for target in self.list() if target.name == name), None)

    def resolve_node_id(self, name: str) -> str:
        """Resolve a graph node name to its persistent node ID for display."""
        return self._graph.nodes[name].node_id

    def validate_target(self, target_name: str | None) -> DeliverResult:
        """Validate a target name and return a ``DeliverResult``.

        Shared by the agent tool and (indirectly) by the REST route.
        Returns ``DeliverResult.ok=True`` only when ``target_name`` is a
        valid downstream node name.
        """
        if target_name is None:
            return DeliverResult.missing_target(self.names())
        if self.get(target_name) is None:
            return DeliverResult.invalid_target(target_name, self.names())
        return DeliverResult.success(target_name)


_DELIVER_PARAMETERS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "target": {
            "type": "string",
            "description": (
                "Exact downstream node name. Required — delivering without "
                "a target is not allowed."
            ),
        },
        "content": {
            "type": "string",
            "description": "Self-contained content for the downstream node.",
        },
    },
    "required": ["content", "target"],
}


class GraphDeliverTool(Tool):
    """Deliver agent content to one named downstream node."""

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
            "\n".join(
                f"  - {target.name}: {target.description}"
                for target in targets
            )
            if targets
            else "  (none)"
        )
        return (
            "Route your work output to a downstream node.\n"
            f"Available targets:\n{available}\n\n"
            "Choose the target that matches your node's purpose. "
            "Read each target's description — it tells you what that "
            "downstream node expects. Tailor your `content` to the "
            "chosen target based on its description. "
            "You MUST specify a target."
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

        result = self._store.validate_target(target_name)
        if not result.ok:
            return result.message

        self._node.deliver(payload, target_name, graph_context)
        # After successful deliver, increment count for DeliverRetryHook
        if agent_context is not None and agent_context.runtime is not None:
            state = agent_context.runtime.state
            if state is not None:
                count = state.custom.get(TurnCustomKey.GRAPH_DELIVER_COUNT, 0)
                state.custom[TurnCustomKey.GRAPH_DELIVER_COUNT] = count + 1
        return result.message


__all__ = [
    "DeliverResult",
    "GraphDeliverTarget",
    "GraphDeliverTargetStore",
    "GraphDeliverTool",
]
