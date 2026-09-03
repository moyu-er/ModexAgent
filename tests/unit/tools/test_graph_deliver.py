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
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity, TurnStateBase
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.tools.graph_deliver import (
    DeliverResult,
    GraphDeliverTarget,
    GraphDeliverTargetStore,
    GraphDeliverTool,
)
from modex_agent.tools.manager import InMemoryToolManager
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


def _compiled_graph(
    researcher: _AgentNode | None = None,
) -> tuple[Any, _AgentNode]:
    graph: Graph[Any] = Graph("deliver-test")
    current = _AgentNode("planner", "Planning agent")
    researcher = researcher or _AgentNode("researcher", "Research agent")
    graph.add_node("planner", current)
    graph.add_node("researcher", researcher)
    graph.add_node("formatter", _GenericNode())
    graph.add_edge(GraphNode.START, "planner")
    graph.add_edge("planner", "researcher")
    graph.add_edge("planner", "formatter")
    graph.add_edge("planner", GraphNode.END)
    return graph.compile(), current


def _agent_context(
    graph_context: GraphContext[Any] | None,
    *,
    runtime: AgentRuntime | None = None,
) -> AgentContext:
    return AgentContext(
        system_prompt="",
        history=MagicMock(spec=MessageHistory),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("test.planner"),
        graph_context=graph_context,
        runtime=runtime,
    )


def _make_runtime() -> AgentRuntime:
    state = TurnStateBase(
        identity=TurnIdentity(
            agent_id="planner",
            session=SessionInfo.from_str("test.planner"),
            turn_id="turn-1",
        ),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
    )
    return AgentRuntime(services=AgentRuntimeServices(), state=state)


def test_target_is_frozen_and_forbids_extra_fields() -> None:
    target = GraphDeliverTarget(name="researcher", description="Research agent")

    with pytest.raises(ValidationError):
        GraphDeliverTarget(
            name="researcher",
            description="Research agent",
            unexpected=True,  # type: ignore[call-arg]
        )

    with pytest.raises(ValidationError):
        target.name = "writer"


def test_deliver_result_is_frozen_and_forbids_extra_fields() -> None:
    result = DeliverResult.success("researcher")

    assert result.model_dump() == {
        "ok": True,
        "target": "researcher",
        "message": "Delivered to 'researcher'.",
    }
    with pytest.raises(ValidationError):
        DeliverResult(
            ok=True,
            target="researcher",
            message="Delivered to 'researcher'.",
            unexpected=True,  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        result.ok = False


def test_deliver_result_factories_bind_to_subclass() -> None:
    class SpecializedDeliverResult(DeliverResult):
        pass

    assert type(SpecializedDeliverResult.missing_target([])) is SpecializedDeliverResult
    assert type(SpecializedDeliverResult.invalid_target("writer", [])) is SpecializedDeliverResult
    assert type(SpecializedDeliverResult.success("writer")) is SpecializedDeliverResult


def test_store_lists_downstream_targets_including_end() -> None:
    compiled, _ = _compiled_graph()
    store = GraphDeliverTargetStore(compiled, "planner")

    targets = store.list()

    assert targets == [
        GraphDeliverTarget(name="researcher", description="Research agent"),
        GraphDeliverTarget(name="formatter", description="Graph node 'formatter'"),
        GraphDeliverTarget(
            name=GraphNode.END,
            description=(
                "Terminal node. Collects all upstream deliveries in "
                "delivery order and concatenates them into the "
                "graph's final reply — your content becomes one "
                "block the user reads directly. See the 'Final "
                "Reply' pattern in your system guidance for how "
                "to write it.\n\n"
                "If multiple nodes deliver to __end__, each "
                "contributes one block; delivery order (not "
                "topology order) sets block order. Deliver here "
                "only when your task is fully complete, never "
                "together with another target in the same turn."
            ),
        ),
    ]
    assert store.get("researcher") == targets[0]
    assert store.get(GraphNode.END) == targets[2]


def test_store_degrades_description_sentinel_to_graph_node_label() -> None:
    compiled, _ = _compiled_graph(
        _AgentNode("planner", AgentNode.DESCRIPTION_NOT_FOUND)
    )
    store = GraphDeliverTargetStore(compiled, "planner")

    target = store.get("researcher")

    assert target is not None
    assert target.description == "Graph node 'researcher'"


def test_store_resolves_target_node_id() -> None:
    compiled, _ = _compiled_graph()
    store = GraphDeliverTargetStore(compiled, "planner")

    node_id = store.resolve_node_id("researcher")

    assert node_id == compiled.nodes["researcher"].node_id


def test_description_lists_targets_ids_and_auto_deliver_behavior() -> None:
    compiled, current = _compiled_graph()
    tool = GraphDeliverTool(current, GraphDeliverTargetStore(compiled, "planner"))

    description = tool.description

    assert "You are node: planner" in description
    assert "researcher" in description
    assert "Research agent" in description
    assert "formatter" in description


def test_dynamic_schema_binds_target_enum_to_downstream_names() -> None:
    compiled, current = _compiled_graph()
    tool = GraphDeliverTool(current, GraphDeliverTargetStore(compiled, "planner"))

    schema = tool.get_dynamic_schema()

    assert schema["function"]["name"] == "deliver"
    assert schema["function"]["parameters"]["properties"]["target"]["enum"] == [
        "researcher",
        "formatter",
        "__end__",
    ]
    assert schema["function"]["parameters"]["required"] == ["content", "target"]


async def test_execute_delivers_payload_to_target_name() -> None:
    compiled, current = _compiled_graph()
    current.deliver = MagicMock()  # type: ignore[method-assign]
    graph_context = MagicMock(spec=GraphContext)
    tool = GraphDeliverTool(current, GraphDeliverTargetStore(compiled, "planner"))
    token = current_agent_context.set(_agent_context(graph_context))

    try:
        result = await tool.execute(target="researcher", content="find evidence")
    finally:
        current_agent_context.reset(token)

    assert result == "Delivered to 'researcher'."
    current.deliver.assert_called_once_with(
        GraphPayload(content="find evidence"), "researcher", graph_context
    )


async def test_execute_without_target_returns_error() -> None:
    compiled, current = _compiled_graph()
    current.deliver = MagicMock()  # type: ignore[method-assign]
    graph_context = MagicMock(spec=GraphContext)
    tool = GraphDeliverTool(current, GraphDeliverTargetStore(compiled, "planner"))
    token = current_agent_context.set(_agent_context(graph_context))

    try:
        result = await tool.execute(content="shared result")
    finally:
        current_agent_context.reset(token)

    assert result.startswith("Error: target is required")
    assert "researcher" in result
    assert "formatter" in result
    current.deliver.assert_not_called()


async def test_execute_rejects_unknown_target() -> None:
    compiled, current = _compiled_graph()
    current.deliver = MagicMock()  # type: ignore[method-assign]
    graph_context = MagicMock(spec=GraphContext)
    tool = GraphDeliverTool(current, GraphDeliverTargetStore(compiled, "planner"))
    token = current_agent_context.set(_agent_context(graph_context))

    try:
        result = await tool.execute(target="invented", content="content")
    finally:
        current_agent_context.reset(token)

    assert result == (
        "Error: 'invented' is not a valid downstream node. "
        "Available: researcher, formatter, __end__."
    )
    current.deliver.assert_not_called()


async def test_execute_rejects_missing_graph_context() -> None:
    compiled, current = _compiled_graph()
    current.deliver = MagicMock()  # type: ignore[method-assign]
    tool = GraphDeliverTool(current, GraphDeliverTargetStore(compiled, "planner"))
    token = current_agent_context.set(_agent_context(None))

    try:
        result = await tool.execute(target="researcher", content="content")
    finally:
        current_agent_context.reset(token)

    assert result == "Error: deliver tool called outside graph context."
    current.deliver.assert_not_called()


async def test_execute_increments_deliver_count_on_success() -> None:
    compiled, current = _compiled_graph()
    current.deliver = MagicMock()  # type: ignore[method-assign]
    graph_context = MagicMock(spec=GraphContext)
    tool = GraphDeliverTool(current, GraphDeliverTargetStore(compiled, "planner"))
    runtime = _make_runtime()
    token = current_agent_context.set(_agent_context(graph_context, runtime=runtime))

    try:
        result = await tool.execute(target="researcher", content="find evidence")
    finally:
        current_agent_context.reset(token)

    assert result == "Delivered to 'researcher'."
    assert runtime.state.custom[TurnCustomKey.GRAPH_DELIVER_COUNT] == 1


async def test_execute_increments_deliver_count_across_two_delivers() -> None:
    compiled, current = _compiled_graph()
    current.deliver = MagicMock()  # type: ignore[method-assign]
    graph_context = MagicMock(spec=GraphContext)
    tool = GraphDeliverTool(current, GraphDeliverTargetStore(compiled, "planner"))
    runtime = _make_runtime()
    token = current_agent_context.set(_agent_context(graph_context, runtime=runtime))

    try:
        await tool.execute(target="researcher", content="first")
        await tool.execute(target="formatter", content="second")
    finally:
        current_agent_context.reset(token)

    assert runtime.state.custom[TurnCustomKey.GRAPH_DELIVER_COUNT] == 2


async def test_execute_does_not_increment_deliver_count_on_failed_deliver() -> None:
    compiled, current = _compiled_graph()
    graph_context = MagicMock(spec=GraphContext)
    tool = GraphDeliverTool(current, GraphDeliverTargetStore(compiled, "planner"))
    runtime = _make_runtime()
    token = current_agent_context.set(_agent_context(graph_context, runtime=runtime))

    try:
        result = await tool.execute(target="invented", content="content")
    finally:
        current_agent_context.reset(token)

    assert "not a valid downstream node" in result
    assert TurnCustomKey.GRAPH_DELIVER_COUNT not in runtime.state.custom
