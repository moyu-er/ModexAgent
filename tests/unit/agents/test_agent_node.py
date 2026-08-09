from __future__ import annotations

import inspect
from dataclasses import fields
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bot.graph.agent_node import BotAgentNode
from bot.graph.agent_node_factory import BotAgentNodeConfig, BotAgentNodeFactory

from modex_agent.agents.agent_node import AgentNode, SessionStrategy
from modex_agent.agents.react.agent import ReActAgent
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.core.session_registry import InMemorySessionRegistry, SessionRegistry
from modex_agent.core.tool_manager import InMemoryToolManager, Tool
from modex_agent.core.types import MessageRole
from modex_agent.memory.history import ListMessageHistory
from modex_agent.pipeline.turn_runner import ReActTurnRunner
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.tools.graph_deliver import GraphDeliverTool
from modex_agent.tools.graph_tool_preset import GraphToolPreset
from modex_graph.constants import GraphNode
from modex_graph.context import GraphContext
from modex_graph.integration import GraphPayload, IntegratedInput, IntegratedPayload
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
) -> MagicMock:
    """Build a mock WorkspaceResolverCell chain returning the given agent instance."""
    mock_agent_pool = MagicMock()
    mock_agent_pool.get.return_value = agent_instance
    mock_agent_pool.session_registry = session_registry or InMemorySessionRegistry()

    mock_pool_instance = MagicMock()
    mock_pool_instance.pool = mock_agent_pool

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
) -> MagicMock:
    """Build a mock AgentInstance with pipeline/context_manager wired."""
    instance = MagicMock()
    instance.descriptor.role_description = role_description
    instance.context_manager = MagicMock()
    instance.pipeline = MagicMock()
    instance.pipeline.agent = agent
    instance.pipeline._turn_context_builder = builder
    if turn_runner is not None:
        instance.pipeline._turn_runner = turn_runner
    return instance


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
        mock_graph = MagicMock()
        mock_graph.nodes = {}
        node._graph_ref = mock_graph

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


class TestBotAgentNodeAutoDeliver:
    def test_has_pending_delivers_false_when_none(self) -> None:
        node = BotAgentNode("a", "p", MagicMock())

        assert node._has_pending_delivers() is False

    def test_has_pending_delivers_true_when_non_empty(self) -> None:
        node = BotAgentNode("a", "p", MagicMock())
        node._pending_delivers = [(GraphPayload(content="x"), "downstream")]

        assert node._has_pending_delivers() is True

    def test_extract_content_from_assistant_message(self) -> None:
        node = BotAgentNode("a", "p", MagicMock())
        result = AgentResult(
            messages=[
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "final answer"},
            ],
        )

        assert node._extract_auto_deliver_content(result) == "final answer"

    def test_extract_content_falls_back_to_result_content(self) -> None:
        node = BotAgentNode("a", "p", MagicMock())
        result = AgentResult(content="fallback only")

        assert node._extract_auto_deliver_content(result) == "fallback only"

    def test_extract_content_strips_think_tags(self) -> None:
        node = BotAgentNode("a", "p", MagicMock())
        result = AgentResult(
            messages=[
                {"role": "assistant", "content": "<think>hidden</think>visible output"},
            ],
        )

        assert node._extract_auto_deliver_content(result) == "visible output"


class TestBotAgentNodeEnsureDeliverTool:
    def test_creates_deliver_tool_lazily(self) -> None:
        node = BotAgentNode("a", "p", MagicMock())
        node.name = "test_node"
        mock_graph = MagicMock()
        mock_graph.nodes = {}
        node._graph_ref = mock_graph

        tool = node._ensure_deliver_tool()

        assert isinstance(tool, GraphDeliverTool)
        assert node._deliver_tool is tool

    def test_reuses_existing_deliver_tool(self) -> None:
        node = BotAgentNode("a", "p", MagicMock())
        node.name = "test_node"
        mock_graph = MagicMock()
        mock_graph.nodes = {}
        node._graph_ref = mock_graph

        first = node._ensure_deliver_tool()
        second = node._ensure_deliver_tool()

        assert first is second


class TestBotAgentNodeExecute:
    @staticmethod
    def _build_execute_mocks() -> tuple[
        MagicMock, MagicMock, MagicMock, MagicMock, AgentResult, MagicMock
    ]:
        """Build the mock chain for execute: agent, builder, agent_context, emitter, result, turn_runner."""
        mock_result = AgentResult(
            content="auto-delivered output",
            messages=[],
            stop_reason=StopReason.COMPLETED,
        )
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=mock_result)

        mock_turn_runner = MagicMock(spec=ReActTurnRunner)
        mock_turn_runner.execute_turn = AsyncMock(return_value=mock_result)

        mock_agent_context = MagicMock()
        mock_agent_context.tool_manager = InMemoryToolManager()
        mock_agent_context.history = MagicMock()
        mock_agent_context.history.append = AsyncMock()
        mock_agent_context.runtime = MagicMock()
        mock_agent_context.runtime.state.custom = {}

        mock_emitter = MagicMock()
        mock_builder = MagicMock()
        mock_builder.assemble = AsyncMock(return_value=MagicMock())
        mock_builder.build_runtime_and_context.return_value = (mock_agent_context, mock_emitter)

        return mock_agent, mock_builder, mock_agent_context, mock_emitter, mock_result, mock_turn_runner

    async def test_execute_runs_full_flow_and_auto_delivers(self) -> None:
        mock_agent, mock_builder, mock_agent_context, mock_emitter, mock_result, mock_turn_runner = (
            self._build_execute_mocks()
        )
        instance = _build_mock_agent_instance(mock_builder, mock_agent, turn_runner=mock_turn_runner)
        resolver = _build_mock_workspace_resolver("default", instance)

        node = BotAgentNode("planner", "default", resolver)
        node.name = "planner_node"
        node.node_id = "node-1"
        mock_graph = MagicMock()
        mock_src = MagicMock()
        mock_src.node_id = "src-id"
        mock_graph.nodes = {"source": mock_src}
        downstream_edge = MagicMock()
        downstream_edge.target = "downstream"
        mock_graph.edges_from.return_value = [downstream_edge]
        node._graph_ref = mock_graph

        mock_ctx = MagicMock()
        mock_ctx.user_input = GraphPayload(content="do the task")

        integrated = IntegratedInput(payloads=[
            IntegratedPayload(source_node="src-id", content=GraphPayload(content="upstream data")),
        ])

        await node.execute(mock_ctx, integrated)

        mock_turn_runner.execute_turn.assert_awaited_once()
        mock_agent_context.history.append.assert_awaited_once()
        assert mock_agent_context.runtime.state.custom[TurnCustomKey.MAX_TURNS] == 3
        assert mock_agent_context.graph_context is mock_ctx
        assert len(node._pending_delivers or []) == 1
        delivered_content, delivered_target = (node._pending_delivers or [])[0]
        assert isinstance(delivered_content, GraphPayload)
        assert delivered_content.content == "auto-delivered output"
        assert delivered_target == "downstream"

    async def test_execute_auto_delivers_to_end_with_multiple_downstream_edges(
        self,
    ) -> None:
        mock_agent, mock_builder, _, _, _, mock_turn_runner = self._build_execute_mocks()
        instance = _build_mock_agent_instance(
            mock_builder,
            mock_agent,
            turn_runner=mock_turn_runner,
        )
        resolver = _build_mock_workspace_resolver("default", instance)
        node = BotAgentNode("planner", "default", resolver)
        node.name = "planner_node"
        node.node_id = "node-1"
        mock_graph = MagicMock()
        first_edge = MagicMock()
        first_edge.target = "first"
        second_edge = MagicMock()
        second_edge.target = "second"
        mock_graph.nodes = {}
        mock_graph.edges_from.return_value = [first_edge, second_edge]
        node._graph_ref = mock_graph
        mock_ctx = MagicMock()
        mock_ctx.user_input = GraphPayload(content="task")

        await node.execute(mock_ctx, IntegratedInput(payloads=[]))

        assert (node._pending_delivers or [])[0][1] == GraphNode.END

    async def test_execute_auto_delivers_to_end_without_downstream_edges(self) -> None:
        mock_agent, mock_builder, _, _, _, mock_turn_runner = self._build_execute_mocks()
        instance = _build_mock_agent_instance(
            mock_builder,
            mock_agent,
            turn_runner=mock_turn_runner,
        )
        resolver = _build_mock_workspace_resolver("default", instance)
        node = BotAgentNode("planner", "default", resolver)
        node.name = "planner_node"
        node.node_id = "node-1"
        mock_graph = MagicMock()
        mock_graph.nodes = {}
        mock_graph.edges_from.return_value = []
        node._graph_ref = mock_graph
        mock_ctx = MagicMock()
        mock_ctx.user_input = GraphPayload(content="task")

        await node.execute(mock_ctx, IntegratedInput(payloads=[]))

        assert (node._pending_delivers or [])[0][1] == GraphNode.END

    async def test_execute_injects_integrated_input_as_reminder(self) -> None:
        mock_agent, mock_builder, mock_agent_context, _, _, mock_turn_runner = self._build_execute_mocks()
        instance = _build_mock_agent_instance(mock_builder, mock_agent, turn_runner=mock_turn_runner)
        resolver = _build_mock_workspace_resolver("default", instance)

        node = BotAgentNode("planner", "default", resolver)
        node.name = "planner_node"
        node.node_id = "node-1"
        mock_graph = MagicMock()
        mock_src = MagicMock()
        mock_src.node_id = "src-id"
        mock_graph.nodes = {"source": mock_src}
        node._graph_ref = mock_graph

        mock_ctx = MagicMock()
        mock_ctx.user_input = GraphPayload(content="task")
        integrated = IntegratedInput(payloads=[
            IntegratedPayload(source_node="src-id", content=GraphPayload(content="injected data")),
        ])

        await node.execute(mock_ctx, integrated)

        append_call = mock_agent_context.history.append.call_args
        appended_msg = append_call.args[0]
        assert appended_msg["role"] == MessageRole.SYSTEM_REMINDER
        assert appended_msg["content"].startswith("<system-reminder>\n")
        assert appended_msg["content"].endswith("\n</system-reminder>")
        assert "injected data" in appended_msg["content"]

    async def test_execute_does_not_append_global_user_input_when_integrated_input_exists(
        self,
    ) -> None:
        mock_agent, mock_builder, _, _, _, mock_turn_runner = self._build_execute_mocks()
        instance = _build_mock_agent_instance(mock_builder, mock_agent, turn_runner=mock_turn_runner)
        resolver = _build_mock_workspace_resolver("default", instance)

        node = BotAgentNode("planner", "default", resolver)
        node.name = "planner_node"
        node.node_id = "node-1"
        mock_graph = MagicMock()
        mock_graph.nodes = {}
        node._graph_ref = mock_graph

        mock_ctx = MagicMock()
        mock_ctx.user_input = GraphPayload(content="global task")
        integrated = IntegratedInput(payloads=[
            IntegratedPayload(source_node="src-id", content=GraphPayload(content="upstream data")),
        ])

        await node.execute(mock_ctx, integrated)

        assemble_call = mock_builder.assemble.call_args
        assert assemble_call.kwargs["input_msg"].content == ""
        assert assemble_call.kwargs["sanitized_content"] is None
        assert assemble_call.kwargs["append_user_message"] is False

    async def test_execute_uses_user_input_reminder_when_integrated_input_is_empty(
        self,
    ) -> None:
        mock_agent, mock_builder, mock_agent_context, _, _, mock_turn_runner = self._build_execute_mocks()
        instance = _build_mock_agent_instance(mock_builder, mock_agent, turn_runner=mock_turn_runner)
        resolver = _build_mock_workspace_resolver("default", instance)

        node = BotAgentNode("planner", "default", resolver)
        node.name = "planner_node"
        node.node_id = "node-1"
        mock_graph = MagicMock()
        mock_graph.nodes = {}
        node._graph_ref = mock_graph

        mock_ctx = MagicMock()
        mock_ctx.user_input = GraphPayload(content="fallback task")

        await node.execute(mock_ctx, IntegratedInput(payloads=[]))

        append_call = mock_agent_context.history.append.call_args
        appended_msg = append_call.args[0]
        assert appended_msg["role"] == MessageRole.SYSTEM_REMINDER
        appended_content = appended_msg["content"]
        assert "[Origin Request]" in appended_content
        assert "fallback task" in appended_content

    async def test_execute_skips_auto_deliver_when_agent_delivered(self) -> None:
        mock_agent, mock_builder, mock_agent_context, mock_emitter, mock_result, mock_turn_runner = (
            self._build_execute_mocks()
        )
        instance = _build_mock_agent_instance(mock_builder, mock_agent, turn_runner=mock_turn_runner)
        resolver = _build_mock_workspace_resolver("default", instance)

        node = BotAgentNode("planner", "default", resolver)
        node.name = "planner_node"
        node.node_id = "node-1"
        mock_graph = MagicMock()
        mock_graph.nodes = {}
        node._graph_ref = mock_graph
        node._pending_delivers = []

        async def _execute_with_deliver(*args: object, **kwargs: object) -> AgentResult:
            node._pending_delivers = [(GraphPayload(content="manual"), "downstream")]
            return mock_result

        mock_turn_runner.execute_turn = AsyncMock(side_effect=_execute_with_deliver)

        mock_ctx = MagicMock()
        mock_ctx.user_input = GraphPayload(content="task")
        integrated = IntegratedInput(payloads=[])

        await node.execute(mock_ctx, integrated)

        mock_turn_runner.execute_turn.assert_awaited_once()
        assert len(node._pending_delivers) == 1
        assert node._pending_delivers[0][0].content == "manual"


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


class _StubTurnRunner(ReActTurnRunner):
    """Minimal ReActTurnRunner returning a fixed AgentResult for execute tests."""

    def __init__(self, result: AgentResult) -> None:
        self._result = result

    async def execute_turn(self, *args: object, **kwargs: object) -> AgentResult:
        return self._result


class TestBotAgentNodeSessionCleanup:

    @staticmethod
    def _build_node(
        registry: SessionRegistry,
        strategy: SessionStrategy,
    ) -> BotAgentNode:
        mock_result = AgentResult(content="output", messages=[])
        mock_agent_context = MagicMock()
        mock_agent_context.tool_manager = InMemoryToolManager()
        mock_agent_context.history = MagicMock()
        mock_agent_context.history.append = AsyncMock()
        mock_emitter = MagicMock()
        mock_builder = MagicMock()
        mock_builder.assemble = AsyncMock(return_value=MagicMock())
        mock_builder.build_runtime_and_context.return_value = (
            mock_agent_context,
            mock_emitter,
        )

        instance = _build_mock_agent_instance(mock_builder, MagicMock())
        instance.pipeline._turn_runner = _StubTurnRunner(mock_result)

        resolver = _build_mock_workspace_resolver(
            "default", instance, session_registry=registry
        )

        node = BotAgentNode("planner", "default", resolver, session_strategy=strategy)
        node.name = "planner_node"
        node.node_id = "node-1"
        mock_graph = MagicMock()
        mock_graph.nodes = {}
        node._graph_ref = mock_graph
        return node

    async def test_per_invocation_session_cleaned_up_after_execute(self) -> None:
        registry = _RecordingSessionRegistry()
        node = self._build_node(registry, SessionStrategy.PER_INVOCATION)

        mock_ctx = MagicMock()
        mock_ctx.user_input = GraphPayload(content="task")
        await node.execute(mock_ctx, IntegratedInput(payloads=[]))

        assert len(registry.registered) == 1
        session_id = registry.registered[0].session_id
        assert await registry.get(session_id) is None

    async def test_cached_session_not_cleaned_up_after_execute(self) -> None:
        registry = _RecordingSessionRegistry()
        node = self._build_node(registry, SessionStrategy.CACHED)

        mock_ctx = MagicMock()
        mock_ctx.user_input = GraphPayload(content="task")
        await node.execute(mock_ctx, IntegratedInput(payloads=[]))

        assert len(registry.registered) == 1
        session_id = registry.registered[0].session_id
        assert await registry.get(session_id) is not None
