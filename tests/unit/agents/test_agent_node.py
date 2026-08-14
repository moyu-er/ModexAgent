from __future__ import annotations

import inspect
from dataclasses import fields
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bot.graph.agent_node import BotAgentNode
from bot.graph.agent_node_factory import BotAgentNodeConfig, BotAgentNodeFactory
from bot.graph.knowledge_config import KnowledgeNodeConfig

from modex_agent.agents.agent_node import AgentNode, SessionStrategy
from modex_agent.agents.react.agent import ReActAgent
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.core.session_registry import InMemorySessionRegistry, SessionRegistry
from modex_agent.core.tool_manager import InMemoryToolManager, Tool
from modex_agent.memory.history import ListMessageHistory
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.pipeline.turn_context_config import GraphTurnArtifacts
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.tools.graph_deliver import GraphDeliverTool
from modex_agent.tools.graph_tool_preset import GraphToolPreset
from modex_graph.constants import FrameworkPayloadSource, GraphNode
from modex_graph.context import GraphContext
from modex_graph.graph import Graph
from modex_graph.integration import GraphPayload, IntegratedInput, IntegratedPayload
from modex_graph.nodes.function_node import FunctionNode
from modex_graph.spec import NodeSpec


class _RecordingSessionRegistry(InMemorySessionRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.registered: list[SessionInfo] = []

    async def register(self, session: SessionInfo) -> None:
        self.registered.append(session)
        await super().register(session)


class _TestAgentNode(AgentNode):
    def __init__(
        self,
        registry: SessionRegistry,
        *,
        session_strategy: SessionStrategy = SessionStrategy.CACHED,
    ) -> None:
        super().__init__(session_strategy=session_strategy)
        self._registry = registry

    def agent_name(self) -> str:
        return "planner"

    async def _resolve_session_registry(self) -> SessionRegistry:
        return self._registry

    async def execute(
        self,
        ctx: GraphContext[Any],
        integrated_input: IntegratedInput,
    ) -> None:
        return None


def _named_tool(name: str) -> Tool:
    tool = MagicMock(spec=Tool)
    tool.name = name
    return tool


def _graph_context() -> GraphContext[Any]:
    return MagicMock(spec=GraphContext)


class TestAgentNodeContract:
    def test_is_abstract(self) -> None:
        assert inspect.isabstract(AgentNode)

    def test_default_session_strategy_is_cached(self) -> None:
        node = _TestAgentNode(_RecordingSessionRegistry())

        assert node._session_strategy is SessionStrategy.CACHED

    def test_initial_runtime_references_are_empty(self) -> None:
        node = _TestAgentNode(_RecordingSessionRegistry())

        assert node._session is None
        assert node._graph_ref is None

    def test_resolve_description_defaults_to_not_found(self) -> None:
        node = _TestAgentNode(_RecordingSessionRegistry())

        assert node.resolve_description() == "[not found]"


class TestAgentNodeSessions:
    async def test_cached_strategy_reuses_registered_session(self) -> None:
        registry = _RecordingSessionRegistry()
        node = _TestAgentNode(registry)
        node.node_id = "node-42"

        first = await node._ensure_session(_graph_context())
        second = await node._ensure_session(_graph_context())

        assert first is second
        assert registry.registered == [first]

    async def test_per_invocation_strategy_registers_new_session_each_time(self) -> None:
        registry = _RecordingSessionRegistry()
        node = _TestAgentNode(
            registry,
            session_strategy=SessionStrategy.PER_INVOCATION,
        )
        node.node_id = "node-42"

        first = await node._ensure_session(_graph_context())
        second = await node._ensure_session(_graph_context())

        assert first is not second
        assert first.session_id != second.session_id
        assert registry.registered == [first, second]
        assert node._session is None

    async def test_create_session_maps_node_and_agent_to_external_id(self) -> None:
        registry = _RecordingSessionRegistry()
        node = _TestAgentNode(registry)
        node.node_id = "node-42"
        expected = SessionIdFactory().create(
            "planner",
            external_id="node-42.planner",
        )

        session = await node._create_session(_graph_context())

        assert session.session_id == expected.session_id
        assert session.agent_name == "planner"
        assert registry.registered == [session]


class TestAgentContextGraphContext:
    def test_graph_context_defaults_to_none(self) -> None:
        graph_context_field = next(
            field for field in fields(AgentContext) if field.name == "graph_context"
        )

        assert graph_context_field.default is None


class TestGraphToolPreset:
    def test_build_copies_base_and_graph_tools(self) -> None:
        base = InMemoryToolManager()
        base_tool = _named_tool("base")
        graph_tool = _named_tool("graph")
        base.register(base_tool)

        result = GraphToolPreset([graph_tool]).build_tool_manager(base)

        assert isinstance(result, InMemoryToolManager)
        assert result.get_tool("base") is base_tool
        assert result.get_tool("graph") is graph_tool

    def test_build_does_not_modify_base_manager(self) -> None:
        base = InMemoryToolManager()
        base.register(_named_tool("base"))

        GraphToolPreset([_named_tool("graph")]).build_tool_manager(base)

        assert base.list_tools() == ["base"]

    def test_built_manager_is_independent_from_base(self) -> None:
        base = InMemoryToolManager()
        base.register(_named_tool("base"))
        result = GraphToolPreset([]).build_tool_manager(base)

        result.register(_named_tool("result-only"))

        assert result.list_tools() == ["base", "result-only"]
        assert base.list_tools() == ["base"]

    def test_graph_tool_replaces_base_tool_with_same_name(self) -> None:
        base = InMemoryToolManager()
        base.register(_named_tool("shared"))
        graph_tool = _named_tool("shared")

        result = GraphToolPreset([graph_tool]).build_tool_manager(base)

        assert result.get_tool("shared") is graph_tool


# BotAgentNode tests


def _build_mock_workspace_resolver(
    pool_name: str,
    agent_instance: MagicMock,
    session_registry: SessionRegistry | None = None,
    tree_manager: MagicMock | None = None,
) -> MagicMock:
    """Build a mock WorkspaceResolverCell chain returning the given agent instance."""
    mock_agent_pool = MagicMock()
    mock_agent_pool.get.return_value = agent_instance
    mock_agent_pool.session_registry = session_registry or InMemorySessionRegistry()

    mock_pool_instance = MagicMock()
    mock_pool_instance.pool = mock_agent_pool
    mock_pool_instance.tree_manager = tree_manager or MagicMock()

    mock_workspace = MagicMock()
    mock_workspace.pools = {pool_name: mock_pool_instance}
    mock_workspace.pool_data = {pool_name: MagicMock()}

    resolver = MagicMock()
    resolver.resolve_workspace.return_value = mock_workspace
    return resolver


def _build_mock_agent_instance(
    builder: MagicMock,
    agent: MagicMock,
    role_description: str = "test role",
    turn_runner: MagicMock | None = None,
    existing_messages: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """Build a mock AgentInstance with pipeline/context_manager wired."""
    instance = MagicMock()
    instance.descriptor.role_description = role_description
    instance.context_manager = MagicMock()
    # _build_graph_input_envelope loads session history to detect re-execution.
    mock_history = MagicMock()
    mock_history.to_list = AsyncMock(return_value=existing_messages or [])
    instance.context_manager.load = AsyncMock(
        return_value=MagicMock(history=mock_history)
    )
    instance.pipeline = MagicMock()
    instance.pipeline.agent = agent
    instance.pipeline._turn_context_builder = builder
    if turn_runner is not None:
        instance.pipeline._turn_runner = turn_runner
    return instance


def _mock_graph_ref() -> MagicMock:
    """Build a mock graph with string attributes for topology rendering."""
    mock_graph = MagicMock()
    mock_graph.name = "test-graph"
    mock_graph.nodes = {}
    mock_graph.edges = []
    mock_graph.edges_from.return_value = []
    return mock_graph


class TestBotAgentNodeBasics:
    def test_agent_name_returns_configured_name(self) -> None:
        node = BotAgentNode("planner", "default", MagicMock())

        assert node.agent_name() == "planner"

    def test_resolve_description_returns_role_description(self) -> None:
        instance = _build_mock_agent_instance(MagicMock(), MagicMock(), "a smart planner")
        resolver = _build_mock_workspace_resolver("default", instance)
        node = BotAgentNode("planner", "default", resolver)

        assert node.resolve_description() == "a smart planner"

    def test_resolve_description_returns_not_found_when_empty(self) -> None:
        instance = _build_mock_agent_instance(MagicMock(), MagicMock(), "")
        resolver = _build_mock_workspace_resolver("default", instance)
        node = BotAgentNode("planner", "default", resolver)

        assert node.resolve_description() == "[not found]"


class TestBotAgentNodeResolveSourceName:
    def test_resolves_known_node_id(self) -> None:
        node = BotAgentNode("a", "p", MagicMock())
        mock_graph = MagicMock()
        mock_node = MagicMock()
        mock_node.node_id = "id-src"
        mock_graph.nodes = {"source": mock_node}
        node._graph_ref = mock_graph

        assert node._resolve_source_name("id-src") == "source"

    def test_returns_node_id_when_not_found(self) -> None:
        node = BotAgentNode("a", "p", MagicMock())
        node._graph_ref = _mock_graph_ref()

        assert node._resolve_source_name("unknown") == "unknown"

    def test_returns_node_id_when_no_graph_ref(self) -> None:
        node = BotAgentNode("a", "p", MagicMock())

        assert node._resolve_source_name("anything") == "anything"


class TestBotAgentNodeFormatIntegratedInput:
    def test_formats_graph_payload_content(self) -> None:
        node = BotAgentNode("a", "p", MagicMock())
        mock_graph = MagicMock()
        mock_node = MagicMock()
        mock_node.node_id = "src-id"
        mock_graph.nodes = {"src": mock_node}
        node._graph_ref = mock_graph

        integrated = IntegratedInput(payloads=[
            IntegratedPayload(source_node="src-id", content=GraphPayload(content="hello world")),
        ])

        result = node._format_integrated_input(integrated)

        assert "[Input from graph node" in result
        assert "hello world" in result
        assert "src" in result

    def test_formats_non_graph_payload_content(self) -> None:
        node = BotAgentNode("a", "p", MagicMock())
        mock_graph = MagicMock()
        mock_node = MagicMock()
        mock_node.node_id = "src-id"
        mock_graph.nodes = {"src": mock_node}
        node._graph_ref = mock_graph

        integrated = IntegratedInput(payloads=[
            IntegratedPayload(source_node="src-id", content="plain text"),
        ])

        result = node._format_integrated_input(integrated)

        assert "plain text" in result

    def test_groups_by_source_node(self) -> None:
        node = BotAgentNode("a", "p", MagicMock())
        mock_graph = MagicMock()
        mock_node = MagicMock()
        mock_node.node_id = "src-id"
        mock_graph.nodes = {"src": mock_node}
        node._graph_ref = mock_graph

        integrated = IntegratedInput(payloads=[
            IntegratedPayload(source_node="src-id", content=GraphPayload(content="part 1")),
            IntegratedPayload(source_node="src-id", content=GraphPayload(content="part 2")),
        ])

        result = node._format_integrated_input(integrated)

        assert "part 1" in result
        assert "part 2" in result
        assert result.count("[Input from graph node 'src']") == 1

    def test_empty_payloads_returns_empty_string(self) -> None:
        node = BotAgentNode("a", "p", MagicMock())
        integrated = IntegratedInput(payloads=[])

        assert node._format_integrated_input(integrated) == ""


class TestBotAgentNodeTopologySection:
    def test_build_topology_section(self) -> None:
        graph: Graph[Any] = Graph("workflow-test")
        planner = BotAgentNode("planner", "default", MagicMock())
        researcher = BotAgentNode("researcher", "default", MagicMock())
        graph.add_node("planner", planner)
        graph.add_node("researcher", researcher)
        graph.add_edge(GraphNode.START, "planner")
        graph.add_edge("planner", "researcher")
        graph.add_edge("researcher", GraphNode.END)
        planner._graph_ref = graph.compile()

        section = planner._build_topology_section()

        assert "Graph: workflow-test" in section
        assert "You are node: **planner**" in section
        assert "- planner" in section
        assert "- researcher" in section
        assert "- __start__ → planner" in section
        assert "- planner → researcher" in section
        assert "- researcher → __end__" in section
        assert "Your upstream (nodes that deliver to you): __start__" in section
        assert "Your downstream (nodes you can deliver to): researcher" in section
        assert "Origin Request: the user's input that triggered this graph run." in section
        assert "It enters through __start__" in section

    def test_build_topology_section_none_graph_ref(self) -> None:
        node = BotAgentNode("planner", "default", MagicMock())
        node._graph_ref = None

        assert node._build_topology_section() == ""

    def test_build_topology_section_start_end_labels(self) -> None:
        graph: Graph[Any] = Graph("labels-test")
        planner = BotAgentNode("planner", "default", MagicMock())
        graph.add_node("planner", planner)
        graph.add_node("worker", FunctionNode(lambda ctx: ctx))
        graph.add_edge(GraphNode.START, "planner")
        graph.add_edge("planner", "worker")
        graph.add_edge("worker", GraphNode.END)
        planner._graph_ref = graph.compile()

        section = planner._build_topology_section()

        assert "- __start__ (entry — receives Origin Request)" in section
        assert "- __end__ (terminal — collects all upstream deliveries in order" in section
        assert "- worker" in section.splitlines()
        assert "(agent)" not in section

    def test_build_topology_section_current_node_highlight(self) -> None:
        graph: Graph[Any] = Graph("highlight-test")
        planner = BotAgentNode("planner", "default", MagicMock())
        graph.add_node("planner", planner)
        graph.add_edge(GraphNode.START, "planner")
        graph.add_edge("planner", GraphNode.END)
        planner._graph_ref = graph.compile()

        section = planner._build_topology_section()

        assert "- planner ← YOU ARE HERE" in section.splitlines()

    def test_format_integrated_input_with_upstream_desc(self) -> None:
        upstream_instance = _build_mock_agent_instance(
            MagicMock(),
            MagicMock(),
            role_description="evidence researcher",
        )
        upstream_resolver = _build_mock_workspace_resolver("default", upstream_instance)
        upstream = BotAgentNode("researcher", "default", upstream_resolver)
        current = BotAgentNode("writer", "default", MagicMock())
        graph: Graph[Any] = Graph("role-test")
        graph.add_node("researcher", upstream)
        graph.add_node("writer", current)
        graph.add_edge(GraphNode.START, "researcher")
        graph.add_edge("researcher", "writer")
        graph.add_edge("writer", GraphNode.END)
        current._graph_ref = graph.compile()
        integrated = IntegratedInput(
            payloads=[
                IntegratedPayload(
                    source_node=upstream.node_id,
                    content=GraphPayload(content="research result"),
                ),
            ]
        )

        result = current._format_integrated_input(integrated)

        assert "(upstream node, role: evidence researcher)" in result

    def test_format_integrated_input_non_agent_upstream(self) -> None:
        upstream = FunctionNode(lambda ctx: ctx)
        current = BotAgentNode("writer", "default", MagicMock())
        graph: Graph[Any] = Graph("function-test")
        graph.add_node("loader", upstream)
        graph.add_node("writer", current)
        graph.add_edge(GraphNode.START, "loader")
        graph.add_edge("loader", "writer")
        graph.add_edge("writer", GraphNode.END)
        current._graph_ref = graph.compile()
        integrated = IntegratedInput(
            payloads=[
                IntegratedPayload(
                    source_node=upstream.node_id,
                    content=GraphPayload(content="loaded data"),
                ),
            ]
        )

        result = current._format_integrated_input(integrated)

        assert "(upstream node):" in result
        assert "role:" not in result

    def test_format_integrated_input_missing_upstream(self) -> None:
        first = FunctionNode(lambda ctx: ctx)
        second = FunctionNode(lambda ctx: ctx)
        current = BotAgentNode("writer", "default", MagicMock())
        graph: Graph[Any] = Graph("missing-input-test")
        graph.add_node("first", first)
        graph.add_node("second", second)
        graph.add_node("writer", current)
        graph.add_edge(GraphNode.START, "first")
        graph.add_edge("first", "writer")
        graph.add_edge("first", "second")
        graph.add_edge("second", "writer")
        graph.add_edge("writer", GraphNode.END)
        current._graph_ref = graph.compile()
        integrated = IntegratedInput(
            payloads=[
                IntegratedPayload(
                    source_node=first.node_id,
                    content=GraphPayload(content="first result"),
                ),
            ]
        )

        result = current._format_integrated_input(integrated)

        assert "[Upstream Status]" in result
        assert "- first: delivered" in result
        assert (
            "- second: no input — path not activated in this run, no further "
            "input expected. Proceed with received input."
        ) in result

    def test_format_integrated_input_no_upstream(self) -> None:
        current = BotAgentNode("planner", "default", MagicMock())
        graph: Graph[Any] = Graph("entry-test")
        graph.add_node("planner", current)
        graph.add_edge(GraphNode.START, "planner")
        graph.add_edge("planner", GraphNode.END)
        current._graph_ref = graph.compile()

        result = current._format_integrated_input(IntegratedInput(payloads=[]))

        assert "[Upstream Status]" not in result

    def test_format_integrated_input_no_payloads_with_upstream(self) -> None:
        upstream = FunctionNode(lambda ctx: ctx)
        current = BotAgentNode("writer", "default", MagicMock())
        graph: Graph[Any] = Graph("empty-input-test")
        graph.add_node("loader", upstream)
        graph.add_node("writer", current)
        graph.add_edge(GraphNode.START, "loader")
        graph.add_edge("loader", "writer")
        graph.add_edge("writer", GraphNode.END)
        current._graph_ref = graph.compile()

        result = current._format_integrated_input(IntegratedInput(payloads=[]))

        assert result == (
            "[Upstream Status]\n"
            "- loader: no input — path not activated in this run, no further input "
            "expected. Proceed with received input."
        )

    def test_start_payload_is_skipped_not_duplicated(self) -> None:
        """__start__ payloads must not appear as [Input from graph node '__start__'].
        The [Origin Request] block in execute() already carries this content."""
        graph: Graph[Any] = Graph("test-skip-start")
        entry = BotAgentNode("entry", "default", MagicMock())
        graph.add_node("entry", entry)
        graph.add_edge(GraphNode.START, "entry")
        graph.add_edge("entry", GraphNode.END)
        compiled = graph.compile()
        entry._graph_ref = compiled

        start_node = compiled.nodes[GraphNode.START]
        start_id = start_node.node_id

        integrated = IntegratedInput(payloads=[
            IntegratedPayload(source_node=start_id, content=GraphPayload(content="user input")),
        ])

        result = entry._format_integrated_input(integrated)

        assert "[Input from graph node '__start__']" not in result
        assert "user input" not in result
        assert "[Upstream Status]" not in result

    def test_framework_sentinel_annotated_and_status_skipped(self) -> None:
        """Framework sentinel payloads are annotated as framework feedback,
        and [Upstream Status] is skipped (it would falsely claim real upstreams
        delivered nothing)."""
        graph: Graph[Any] = Graph("test-framework")
        worker = BotAgentNode("worker", "default", MagicMock())
        feeder = BotAgentNode("feeder", "default", MagicMock())
        graph.add_node("worker", worker)
        graph.add_node("feeder", feeder)
        graph.add_edge(GraphNode.START, "feeder")
        graph.add_edge("feeder", "worker")
        graph.add_edge("worker", GraphNode.END)
        compiled = graph.compile()
        worker._graph_ref = compiled

        integrated = IntegratedInput(payloads=[
            IntegratedPayload(
                source_node=FrameworkPayloadSource.FRAMEWORK.value,
                content={"error": "no deliver produced"},
            ),
        ])

        result = worker._format_integrated_input(integrated)

        assert "[Input from graph node '__framework__']" in result
        assert "framework feedback" in result
        assert "[Upstream Status]" not in result


class TestAgentNodeIntegrateUpstreamIdempotency:
    """AgentNode._integrate_upstream must always filter CONSUMED_PENDING.

    Agent session memory persists upstream input across invocations.
    On crash recovery, CONSUMED_PENDING delivers (consumed by the crashed
    invocation) must NOT be re-consumed — they would duplicate the
    SYSTEM_REMINDER in the agent's session history.
    """

    def test_consumed_pending_filtered_on_non_resume(self) -> None:
        from modex_graph.constants import DeliverConsumptionStatus
        from modex_graph.integration import IntegratedPayload

        node = BotAgentNode("worker", "default", MagicMock())
        node.name = "worker"
        node.node_id = "node-worker"

        mock_coordinator = MagicMock()
        pending_record = MagicMock()
        pending_record.status = DeliverConsumptionStatus.PENDING
        pending_record.deliver_id = 101
        pending_record.source_node_id = "src-id"
        pending_record.content = "new deliver"

        consumed_pending_record = MagicMock()
        consumed_pending_record.status = DeliverConsumptionStatus.CONSUMED_PENDING
        consumed_pending_record.deliver_id = 100
        consumed_pending_record.source_node_id = "src-id"
        consumed_pending_record.content = "old deliver"

        mock_coordinator.collect_consumable_delivers.return_value = [
            consumed_pending_record,
            pending_record,
        ]

        result = node._integrate_upstream(
            mock_coordinator,
            MagicMock(invocation_id=42),
            resume_snapshot=None,
        )

        mock_coordinator.mark_delivers_consumed.assert_called_once_with(
            "node-worker", [101], 42
        )
        assert len(result.payloads) == 1
        assert result.payloads[0].content == "new deliver"

    def test_empty_when_all_consumed_pending(self) -> None:
        from modex_graph.constants import DeliverConsumptionStatus

        node = BotAgentNode("worker", "default", MagicMock())
        node.name = "worker"
        node.node_id = "node-worker"

        mock_coordinator = MagicMock()
        consumed_record = MagicMock()
        consumed_record.status = DeliverConsumptionStatus.CONSUMED_PENDING
        consumed_record.deliver_id = 100
        consumed_record.source_node_id = "src-id"
        consumed_record.content = "already injected"

        mock_coordinator.collect_consumable_delivers.return_value = [consumed_record]

        result = node._integrate_upstream(
            mock_coordinator,
            MagicMock(invocation_id=42),
            resume_snapshot=None,
        )

        mock_coordinator.mark_delivers_consumed.assert_not_called()
        assert len(result.payloads) == 0

    def test_resume_snapshot_still_prepended(self) -> None:
        from modex_graph.constants import DeliverConsumptionStatus, FrameworkPayloadSource

        node = BotAgentNode("worker", "default", MagicMock())
        node.name = "worker"
        node.node_id = "node-worker"

        mock_coordinator = MagicMock()
        mock_coordinator.collect_consumable_delivers.return_value = []

        snapshot = {"state": "resumed"}
        result = node._integrate_upstream(
            mock_coordinator,
            MagicMock(invocation_id=42),
            resume_snapshot=snapshot,
        )

        assert len(result.payloads) == 1
        assert result.payloads[0].source_node == FrameworkPayloadSource.RESUME
        assert result.payloads[0].content == snapshot


class TestBotAgentNodeBuildGraphArtifacts:
    def test_returns_correct_artifacts(self) -> None:
        instance = _build_mock_agent_instance(MagicMock(), MagicMock(), "a planner")
        resolver = _build_mock_workspace_resolver("default", instance)
        node = BotAgentNode("planner", "default", resolver)
        node.name = "planner_node"
        node.node_id = "node-1"
        node._graph_ref = _mock_graph_ref()

        ctx = MagicMock(spec=GraphContext)
        ctx.graph_instance_id = None

        artifacts = node._build_graph_artifacts(ctx)

        assert isinstance(artifacts, GraphTurnArtifacts)
        assert "test-graph" in artifacts.topology_section
        assert artifacts.node_description == "a planner"
        assert artifacts.knowledge_dir is None
        assert artifacts.knowledge_config is node._knowledge_config

    def test_knowledge_dir_created_when_enabled(self, tmp_path: Path) -> None:
        instance = _build_mock_agent_instance(MagicMock(), MagicMock())
        resolver = _build_mock_workspace_resolver("default", instance)
        knowledge_config = KnowledgeNodeConfig(enabled=True)
        node = BotAgentNode(
            "planner", "default", resolver, knowledge_config=knowledge_config
        )
        node.name = "planner_node"
        node.node_id = "node-1"
        node._graph_ref = _mock_graph_ref()

        mock_workspace = resolver.resolve_workspace.return_value
        mock_workspace.ctx.paths.graph_instance_knowledge_dir.return_value = (
            tmp_path / "knowledge"
        )

        ctx = MagicMock(spec=GraphContext)
        ctx.graph_instance_id = 42

        artifacts = node._build_graph_artifacts(ctx)

        assert artifacts.knowledge_dir == tmp_path / "knowledge"
        assert artifacts.knowledge_dir.exists()


class TestBotAgentNodeBuildGraphInputEnvelope:
    async def test_first_execution_includes_origin_request(self) -> None:
        instance = _build_mock_agent_instance(
            MagicMock(), MagicMock(), existing_messages=[]
        )
        resolver = _build_mock_workspace_resolver("default", instance)
        node = BotAgentNode("planner", "default", resolver)
        node.name = "planner_node"
        node.node_id = "node-1"
        node._graph_ref = _mock_graph_ref()

        ctx = MagicMock(spec=GraphContext)
        ctx.user_input = GraphPayload(content="do the task")
        ctx.graph_instance_id = 42
        session = SessionInfo.from_str("conv123.planner")

        envelope = await node._build_graph_input_envelope(
            ctx, IntegratedInput(payloads=[]), session
        )

        assert envelope.message_type == AgentMessageType.EXTERNAL_INPUT
        assert "[Origin Request]" in envelope.payload["content"]
        assert "do the task" in envelope.payload["content"]
        assert envelope.metadata["graph_instance_id"] == 42

    async def test_re_execution_skips_origin_request(self) -> None:
        instance = _build_mock_agent_instance(
            MagicMock(),
            MagicMock(),
            existing_messages=[{"role": "user", "content": "prior message"}],
        )
        resolver = _build_mock_workspace_resolver("default", instance)
        node = BotAgentNode("planner", "default", resolver)
        node.name = "planner_node"
        node.node_id = "node-1"
        node._graph_ref = _mock_graph_ref()

        ctx = MagicMock(spec=GraphContext)
        ctx.user_input = GraphPayload(content="do the task")
        ctx.graph_instance_id = 42
        session = SessionInfo.from_str("conv123.planner")

        envelope = await node._build_graph_input_envelope(
            ctx, IntegratedInput(payloads=[]), session
        )

        assert "[Origin Request]" not in envelope.payload["content"]

    async def test_envelope_addresses_and_session(self) -> None:
        instance = _build_mock_agent_instance(MagicMock(), MagicMock())
        resolver = _build_mock_workspace_resolver("default", instance)
        node = BotAgentNode("planner", "default", resolver)
        node.name = "planner_node"
        node.node_id = "node-1"
        node._graph_ref = _mock_graph_ref()

        ctx = MagicMock(spec=GraphContext)
        ctx.user_input = None
        ctx.graph_instance_id = None
        session = SessionInfo.from_str("conv123.planner")

        envelope = await node._build_graph_input_envelope(
            ctx, IntegratedInput(payloads=[]), session
        )

        assert envelope.source.name == "planner_node"
        assert envelope.target is not None
        assert envelope.target.name == "planner"
        assert envelope.session_id == "conv123"
        assert envelope.agent_session_id == "conv123.planner"

    async def test_upstream_input_included_in_content(self) -> None:
        instance = _build_mock_agent_instance(MagicMock(), MagicMock())
        resolver = _build_mock_workspace_resolver("default", instance)
        node = BotAgentNode("planner", "default", resolver)
        node.name = "planner_node"
        node.node_id = "node-1"
        mock_graph = _mock_graph_ref()
        mock_src = MagicMock()
        mock_src.node_id = "src-id"
        mock_graph.nodes = {"source": mock_src}
        node._graph_ref = mock_graph

        ctx = MagicMock(spec=GraphContext)
        ctx.user_input = GraphPayload(content="origin task")
        ctx.graph_instance_id = 42
        session = SessionInfo.from_str("conv123.planner")

        integrated = IntegratedInput(payloads=[
            IntegratedPayload(
                source_node="src-id", content=GraphPayload(content="upstream data")
            ),
        ])

        envelope = await node._build_graph_input_envelope(ctx, integrated, session)

        assert "upstream data" in envelope.payload["content"]
        assert "[Origin Request]" in envelope.payload["content"]


class TestBotAgentNodeEnsureDeliverTool:
    def test_creates_deliver_tool_lazily(self) -> None:
        node = BotAgentNode("a", "p", MagicMock())
        node.name = "test_node"
        node._graph_ref = _mock_graph_ref()

        tool = node._ensure_deliver_tool()

        assert isinstance(tool, GraphDeliverTool)
        assert node._deliver_tool is tool

    def test_reuses_existing_deliver_tool(self) -> None:
        node = BotAgentNode("a", "p", MagicMock())
        node.name = "test_node"
        node._graph_ref = _mock_graph_ref()

        first = node._ensure_deliver_tool()
        second = node._ensure_deliver_tool()

        assert first is second


class TestBotAgentNodeExecute:
    @staticmethod
    def _build_execute_setup(
        existing_messages: list[dict[str, Any]] | None = None,
    ) -> tuple[BotAgentNode, MagicMock, MagicMock]:
        """Build a node + mock tree for thin-shell execute tests."""
        instance = _build_mock_agent_instance(
            MagicMock(), MagicMock(), existing_messages=existing_messages
        )
        mock_tree = MagicMock()
        mock_tree.deliver = AsyncMock()
        mock_tree.wait_quiesce = AsyncMock()
        mock_tree.tree_id_for_session = AsyncMock(return_value="tree-1")
        resolver = _build_mock_workspace_resolver(
            "default", instance, tree_manager=mock_tree
        )

        node = BotAgentNode(
            "planner",
            "default",
            resolver,
            knowledge_config=KnowledgeNodeConfig(enabled=False),
        )
        node.name = "planner_node"
        node.node_id = "node-1"
        node._graph_ref = _mock_graph_ref()
        return node, mock_tree, instance

    async def test_execute_calls_tree_deliver_with_envelope(self) -> None:
        node, mock_tree, _ = self._build_execute_setup()

        ctx = MagicMock(spec=GraphContext)
        ctx.user_input = GraphPayload(content="do the task")
        ctx.graph_instance_id = 42
        ctx.user_data = {}

        await node.execute(ctx, IntegratedInput(payloads=[]))

        mock_tree.deliver.assert_awaited_once()
        call_args = mock_tree.deliver.call_args
        assert call_args.kwargs["track_consume"] is True
        envelope = call_args.args[1]
        assert isinstance(envelope, AgentMessageEnvelope)
        assert envelope.message_type == AgentMessageType.EXTERNAL_INPUT
        assert "[Origin Request]" in envelope.payload["content"]
        assert envelope.metadata["graph_instance_id"] == 42

    async def test_execute_calls_tree_wait_quiesce(self) -> None:
        node, mock_tree, _ = self._build_execute_setup()

        ctx = MagicMock(spec=GraphContext)
        ctx.user_input = GraphPayload(content="task")
        ctx.graph_instance_id = 42
        ctx.user_data = {}

        await node.execute(ctx, IntegratedInput(payloads=[]))

        mock_tree.wait_quiesce.assert_awaited_once_with("tree-1")

    async def test_execute_does_not_call_runner_execute_turn(self) -> None:
        node, mock_tree, instance = self._build_execute_setup()

        ctx = MagicMock(spec=GraphContext)
        ctx.user_input = GraphPayload(content="task")
        ctx.graph_instance_id = 42
        ctx.user_data = {}

        await node.execute(ctx, IntegratedInput(payloads=[]))

        turn_runner = instance.pipeline._turn_runner
        if turn_runner is not None and hasattr(turn_runner, "execute_turn"):
            turn_runner.execute_turn.assert_not_called()

    async def test_execute_does_not_auto_deliver(self) -> None:
        node, mock_tree, _ = self._build_execute_setup()

        ctx = MagicMock(spec=GraphContext)
        ctx.user_input = GraphPayload(content="task")
        ctx.graph_instance_id = 42
        ctx.user_data = {}

        await node.execute(ctx, IntegratedInput(payloads=[]))

        assert not node._pending_delivers

    async def test_execute_stores_artifacts_in_user_data(self) -> None:
        node, mock_tree, _ = self._build_execute_setup()

        ctx = GraphContext(
            state=MagicMock(),
            runtime=MagicMock(),
            coordinator=MagicMock(),
            graph_instance_id=42,
            user_input=GraphPayload(content="task"),
        )
        ctx.user_data = {}

        await node.execute(ctx, IntegratedInput(payloads=[]))

        artifacts = ctx.user_data.get("node_artifacts", {}).get("planner_node")
        assert artifacts is not None
        assert isinstance(artifacts, GraphTurnArtifacts)
        assert artifacts.node_description == "test role"

    async def test_execute_stores_artifacts_on_plain_graph_context(self) -> None:
        node, mock_tree, _ = self._build_execute_setup()

        ctx = MagicMock(spec=GraphContext)
        ctx.user_input = GraphPayload(content="task")
        ctx.graph_instance_id = 42
        ctx.user_data = {}

        await node.execute(ctx, IntegratedInput(payloads=[]))

        assert "node_artifacts" in ctx.user_data


class TestReActAgentCompileBudget:
    async def test_compile_budget_scales_with_runtime_max_turns(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = SessionInfo.from_str("test.agent")
        state = ReActTurnState(
            identity=TurnIdentity(agent_id="test", session=session, turn_id="turn-1"),
            agent_kind=AgentKind.REACT,
            phase=TurnPhase.CREATED,
        )
        state.custom[TurnCustomKey.MAX_TURNS] = 3
        runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
        context = AgentContext(
            system_prompt="",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session=session,
            max_iterations=5,
            identity=state.identity,
            runtime=runtime,
        )
        graph_builder = MagicMock()
        compiled_graph = MagicMock()
        compiled_graph.nodes = {}
        graph_builder.compile.return_value = compiled_graph
        monkeypatch.setattr(
            "modex_agent.agents.react.graph.build_react_graph",
            MagicMock(return_value=graph_builder),
        )
        engine = MagicMock()
        engine.run_async = AsyncMock(return_value=state)
        monkeypatch.setattr(
            "modex_graph.engine.GraphEngine",
            MagicMock(return_value=engine),
        )

        await ReActAgent(provider=MagicMock()).run(context, MagicMock())

        graph_builder.compile.assert_called_once_with(max_iterations=70)


class TestBotAgentNodeFactory:
    def test_create_returns_bot_agent_node(self) -> None:
        factory = BotAgentNodeFactory(MagicMock())
        spec = NodeSpec(
            name="n1",
            node_type="bot_agent",
            config={"agent": "planner", "pool": "default"},
        )

        node = factory.create(spec)

        assert isinstance(node, BotAgentNode)
        assert node.agent_name() == "planner"
        assert node._pool_name == "default"

    def test_create_uses_default_pool(self) -> None:
        factory = BotAgentNodeFactory(MagicMock())
        spec = NodeSpec(name="n1", node_type="bot_agent", config={"agent": "planner"})

        node = factory.create(spec)
        assert isinstance(node, BotAgentNode)

        assert node._pool_name == "default"

    def test_config_schema_returns_model(self) -> None:
        factory = BotAgentNodeFactory(MagicMock())

        schema = factory.config_schema()

        assert schema is BotAgentNodeConfig

    def test_config_schema_validates_config(self) -> None:
        schema = BotAgentNodeConfig
        valid = schema.model_validate({"agent": "planner", "pool": "default"})
        assert valid.agent == "planner"
        assert valid.pool == "default"


# P1-5: PER_INVOCATION session cleanup after execute


class TestBotAgentNodeSessionCleanup:

    @staticmethod
    def _build_node(
        registry: SessionRegistry,
        strategy: SessionStrategy,
    ) -> BotAgentNode:
        mock_tree = MagicMock()
        mock_tree.deliver = AsyncMock()
        mock_tree.wait_quiesce = AsyncMock()
        mock_tree.tree_id_for_session = AsyncMock(return_value="tree-1")

        instance = _build_mock_agent_instance(MagicMock(), MagicMock())
        resolver = _build_mock_workspace_resolver(
            "default", instance, session_registry=registry, tree_manager=mock_tree
        )

        node = BotAgentNode(
            "planner",
            "default",
            resolver,
            session_strategy=strategy,
            knowledge_config=KnowledgeNodeConfig(enabled=False),
        )
        node.name = "planner_node"
        node.node_id = "node-1"
        node._graph_ref = _mock_graph_ref()
        return node

    async def test_per_invocation_session_cleaned_up_after_execute(self) -> None:
        registry = _RecordingSessionRegistry()
        node = self._build_node(registry, SessionStrategy.PER_INVOCATION)

        mock_ctx = MagicMock(spec=GraphContext)
        mock_ctx.user_input = GraphPayload(content="task")
        mock_ctx.graph_instance_id = 42
        mock_ctx.user_data = {}
        await node.execute(mock_ctx, IntegratedInput(payloads=[]))

        assert len(registry.registered) == 1
        session_id = registry.registered[0].session_id
        assert await registry.get(session_id) is None

    async def test_cached_session_not_cleaned_up_after_execute(self) -> None:
        registry = _RecordingSessionRegistry()
        node = self._build_node(registry, SessionStrategy.CACHED)

        mock_ctx = MagicMock(spec=GraphContext)
        mock_ctx.user_input = GraphPayload(content="task")
        mock_ctx.graph_instance_id = 42
        mock_ctx.user_data = {}
        await node.execute(mock_ctx, IntegratedInput(payloads=[]))

        assert len(registry.registered) == 1
        session_id = registry.registered[0].session_id
        assert await registry.get(session_id) is not None
