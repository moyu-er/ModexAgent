from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from modex_agent.agents.agent_node import AgentNode
from modex_agent.core.agent import AgentContext, current_agent_context
from modex_agent.core.history import MessageHistory
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.session_registry import InMemorySessionRegistry, SessionRegistry
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.tools.graph_deliver import (
    GraphDeliverTarget,
    GraphDeliverTargetStore,
    GraphDeliverTool,
)
from modex_graph.constants import GraphNode
from modex_graph.context import GraphContext
from modex_graph.graph import Graph
from modex_graph.integration import GraphPayload, IntegratedInput
from modex_graph.node import Node


class _AgentNode(AgentNode):
    def __init__(self, name: str, description: str) -> None:
        super().__init__()
        self._agent_name = name
        self._description = description
        self._registry = InMemorySessionRegistry()

    def agent_name(self) -> str:
        return self._agent_name

    async def _resolve_session_registry(self) -> SessionRegistry:
        return self._registry

    def resolve_description(self) -> str:
        return self._description

    async def execute(
        self,
        ctx: GraphContext[Any],
        integrated_input: IntegratedInput,
    ) -> None:
        return None


class _GenericNode(Node[Any]):
    async def execute(
        self,
        ctx: GraphContext[Any],
        integrated_input: IntegratedInput,
    ) -> None:
        return None


def _compiled_graph() -> tuple[Any, _AgentNode]:
    graph: Graph[Any] = Graph("deliver-test")
    current = _AgentNode("planner", "Planning agent")
    researcher = _AgentNode("researcher", "Research agent")
    graph.add_node("planner", current)
    graph.add_node("researcher", researcher)
    graph.add_node("formatter", _GenericNode())
    graph.add_edge(GraphNode.START, "planner")
    graph.add_edge("planner", "researcher")
    graph.add_edge("planner", "formatter")
    graph.add_edge("planner", GraphNode.END)
    return graph.compile(), current


def _agent_context(graph_context: GraphContext[Any] | None) -> AgentContext:
    return AgentContext(
        system_prompt="",
        history=MagicMock(spec=MessageHistory),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.planner"),
        graph_context=graph_context,
    )


def test_target_is_frozen_and_forbids_extra_fields() -> None:
    target = GraphDeliverTarget(name="researcher", description="Research agent")

    with pytest.raises(ValidationError):
        GraphDeliverTarget(
            name="researcher",
            description="Research agent",
            unexpected=True,
        )

    with pytest.raises(ValidationError):
        target.name = "writer"


def test_store_lists_downstream_targets_without_end() -> None:
    compiled, _ = _compiled_graph()
    store = GraphDeliverTargetStore(compiled, "planner")

    targets = store.list()

    assert targets == [
        GraphDeliverTarget(name="researcher", description="Research agent"),
        GraphDeliverTarget(name="formatter", description="Graph node 'formatter'"),
    ]
    assert store.get("researcher") == targets[0]
    assert store.get(GraphNode.END) is None


def test_store_resolves_target_node_id() -> None:
    compiled, _ = _compiled_graph()
    store = GraphDeliverTargetStore(compiled, "planner")

    node_id = store.resolve_node_id("researcher")

    assert node_id == compiled.nodes["researcher"].node_id


def test_description_lists_targets_ids_and_auto_deliver_behavior() -> None:
    compiled, current = _compiled_graph()
    tool = GraphDeliverTool(current, GraphDeliverTargetStore(compiled, "planner"))

    description = tool.description

    assert "researcher" in description
    assert compiled.nodes["researcher"].node_id in description
    assert "Research agent" in description
    assert "formatter" in description
    assert "auto-deliver" in description


def test_dynamic_schema_binds_target_enum_to_downstream_names() -> None:
    compiled, current = _compiled_graph()
    tool = GraphDeliverTool(current, GraphDeliverTargetStore(compiled, "planner"))

    schema = tool.get_dynamic_schema()

    assert schema["function"]["name"] == "deliver"
    assert schema["function"]["parameters"]["properties"]["target"]["enum"] == [
        "researcher",
        "formatter",
    ]
    assert schema["function"]["parameters"]["required"] == ["content"]


async def test_execute_delivers_payload_to_target_name() -> None:
    compiled, current = _compiled_graph()
    graph_context = MagicMock(spec=GraphContext)
    tool = GraphDeliverTool(current, GraphDeliverTargetStore(compiled, "planner"))
    token = current_agent_context.set(_agent_context(graph_context))

    try:
        result = await tool.execute(target="researcher", content="find evidence")
    finally:
        current_agent_context.reset(token)

    assert result == "Delivered to 'researcher'."
    assert current._pending_delivers == [
        (GraphPayload(content="find evidence"), "researcher"),
    ]


async def test_execute_without_target_uses_auto_deliver() -> None:
    compiled, current = _compiled_graph()
    graph_context = MagicMock(spec=GraphContext)
    tool = GraphDeliverTool(current, GraphDeliverTargetStore(compiled, "planner"))
    token = current_agent_context.set(_agent_context(graph_context))

    try:
        result = await tool.execute(content="shared result")
    finally:
        current_agent_context.reset(token)

    assert result == "Delivered to all downstream nodes."
    assert current._pending_delivers == [(GraphPayload(content="shared result"), None)]


async def test_execute_rejects_unknown_target() -> None:
    compiled, current = _compiled_graph()
    graph_context = MagicMock(spec=GraphContext)
    tool = GraphDeliverTool(current, GraphDeliverTargetStore(compiled, "planner"))
    token = current_agent_context.set(_agent_context(graph_context))

    try:
        result = await tool.execute(target="invented", content="content")
    finally:
        current_agent_context.reset(token)

    assert result == (
        "Error: 'invented' is not a valid downstream node. "
        "Available: researcher, formatter"
    )
    assert current._pending_delivers is None


async def test_execute_rejects_missing_graph_context() -> None:
    compiled, current = _compiled_graph()
    tool = GraphDeliverTool(current, GraphDeliverTargetStore(compiled, "planner"))
    token = current_agent_context.set(_agent_context(None))

    try:
        result = await tool.execute(target="researcher", content="content")
    finally:
        current_agent_context.reset(token)

    assert result == "Error: deliver tool called outside graph context."
    assert current._pending_delivers is None
