from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from bot.graph.agent_node import BotAgentNode
from bot.graph.agent_node_factory import BotAgentNodeConfig, BotAgentNodeFactory

from modex_agent.core.agent import AgentCommKind
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.core.session_registry import InMemorySessionRegistry
from modex_agent.core.tool_manager import InMemoryToolManager, Tool
from modex_agent.pipeline.turn_context_config import (
    GraphKnowledgeConfigurator,
    GraphToolConfigurator,
    GraphTurnArtifacts,
    TurnContextDescriptor,
)
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.tools.graph_knowledge_tool import GraphKnowledgeBaseTool
from modex_agent.workspace.paths import WorkspacePaths
from modex_graph.context import GraphContext
from modex_graph.integration import GraphPayload
from modex_graph.spec import NodeSpec


def _workspace_resolver(tmp_path: Path, *, builder: MagicMock | None = None) -> MagicMock:
    agent_pool = MagicMock()
    agent_pool.session_registry = InMemorySessionRegistry()

    pool_instance = MagicMock()
    pool_instance.pool = agent_pool

    workspace = MagicMock()
    workspace.ctx.paths = WorkspacePaths(root=tmp_path / ".modex")
    workspace.pools = {"default": pool_instance}
    workspace.pool_data = {"default": MagicMock()}

    if builder is not None:
        instance = MagicMock()
        instance.descriptor.role_description = "test role"
        instance.context_manager = MagicMock()
        instance.pipeline = MagicMock()
        instance.pipeline._turn_context_builder = builder
        agent_pool.get.return_value = instance

    resolver = MagicMock()
    resolver.resolve_workspace.return_value = workspace
    return resolver


def _graph_context(graph_instance_id: int | None) -> MagicMock:
    ctx = MagicMock(spec=GraphContext)
    ctx.graph_instance_id = graph_instance_id
    ctx.user_input = GraphPayload(content="task")
    ctx.user_data = None
    return ctx


def test_config_defaults_enable_read_write_knowledge() -> None:
    config = BotAgentNodeConfig.model_validate({"agent": "planner"})

    assert config.knowledge.enabled is True
    assert config.knowledge.require_read is False
    assert config.knowledge.require_write is False


def test_config_validates_custom_knowledge_block() -> None:
    config = BotAgentNodeConfig.model_validate(
        {
            "agent": "planner",
            "knowledge": {
                "enabled": False,
                "require_read": True,
                "require_write": True,
            },
        }
    )

    assert config.knowledge.enabled is False
    assert config.knowledge.require_read is True
    assert config.knowledge.require_write is True


def test_factory_passes_knowledge_config_to_node() -> None:
    resolver = MagicMock()
    factory = BotAgentNodeFactory(resolver)
    spec = NodeSpec(
        name="planner",
        node_type="agent",
        config={
            "agent": "planner",
            "knowledge": {"require_read": True},
        },
    )

    node = factory.create(spec)

    assert isinstance(node, BotAgentNode)
    assert node._knowledge_config.require_read is True


def test_constructor_defaults_knowledge_config_when_none() -> None:
    node = BotAgentNode("planner", "default", MagicMock(), knowledge_config=None)

    assert node._knowledge_config == BotAgentNodeConfig(agent="planner").knowledge


def test_graph_tool_configurator_creates_instance_scoped_tool(tmp_path: Path) -> None:
    knowledge_dir = WorkspacePaths(root=tmp_path / ".modex").graph_instance_knowledge_dir(42)
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    from modex_agent.tools.standard.file_tool import EditFileTool, ReadFileTool, WriteFileTool
    tm = InMemoryToolManager()
    tm.register(ReadFileTool())
    tm.register(WriteFileTool())
    tm.register(EditFileTool())
    ctx = MagicMock()
    ctx.tool_manager = tm
    deliver_tool = MagicMock(spec=Tool)
    deliver_tool.name = "deliver"
    artifacts = GraphTurnArtifacts(
        deliver_tool=deliver_tool,
        topology_section="",
        node_description="planner",
        knowledge_config=BotAgentNodeConfig(agent="planner").knowledge,
        knowledge_dir=knowledge_dir,
    )
    desc = TurnContextDescriptor(
        agent_kind=AgentCommKind.NORMAL,
        execution_strategy=ExecutionStrategyKind.REACT,
        graph_node_name="planner_node",
        graph_instance_id=42,
        is_node_execution=True,
        graph_artifacts=artifacts,
    )
    GraphToolConfigurator().configure(ctx, desc)
    tool = ctx.tool_manager.get_tool("knowledge_base")

    assert isinstance(tool, GraphKnowledgeBaseTool)
    assert knowledge_dir.is_dir()
    action_schema = tool.get_dynamic_schema()["function"]["parameters"]["properties"]["action"]
    assert action_schema["enum"] == ["read", "ls", "grep", "write", "edit"]


def test_graph_tool_configurator_derives_capabilities_from_tools(tmp_path: Path) -> None:
    knowledge_dir = WorkspacePaths(root=tmp_path / ".modex").graph_instance_knowledge_dir(42)
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    from modex_agent.tools.standard.file_tool import ReadFileTool
    tm = InMemoryToolManager()
    tm.register(ReadFileTool())
    ctx = MagicMock()
    ctx.tool_manager = tm
    deliver_tool = MagicMock(spec=Tool)
    deliver_tool.name = "deliver"
    artifacts = GraphTurnArtifacts(
        deliver_tool=deliver_tool,
        topology_section="",
        node_description="planner",
        knowledge_config=BotAgentNodeConfig(agent="planner").knowledge,
        knowledge_dir=knowledge_dir,
    )
    desc = TurnContextDescriptor(
        agent_kind=AgentCommKind.NORMAL,
        execution_strategy=ExecutionStrategyKind.REACT,
        graph_node_name="planner_node",
        graph_instance_id=42,
        is_node_execution=True,
        graph_artifacts=artifacts,
    )
    GraphToolConfigurator().configure(ctx, desc)
    tool = ctx.tool_manager.get_tool("knowledge_base")

    assert isinstance(tool, GraphKnowledgeBaseTool)
    action_schema = tool.get_dynamic_schema()["function"]["parameters"]["properties"]["action"]
    assert "write" not in action_schema["enum"]
    assert "edit" not in action_schema["enum"]
    assert "read" in action_schema["enum"]


def test_build_graph_artifacts_omits_knowledge_dir_when_disabled(tmp_path: Path) -> None:
    builder = MagicMock()
    resolver = _workspace_resolver(tmp_path, builder=builder)
    config = BotAgentNodeConfig.model_validate(
        {"agent": "planner", "knowledge": {"enabled": False}}
    )
    node = BotAgentNode("planner", "default", resolver, knowledge_config=config.knowledge)
    node.name = "planner_node"
    graph = MagicMock()
    graph.nodes = {}
    graph.edges = []
    graph.edges_from.return_value = []
    node._graph_ref = graph

    assert node._build_graph_artifacts(_graph_context(42)).knowledge_dir is None


def test_build_graph_artifacts_omits_knowledge_dir_without_graph_instance(tmp_path: Path) -> None:
    builder = MagicMock()
    resolver = _workspace_resolver(tmp_path, builder=builder)
    node = BotAgentNode("planner", "default", resolver)
    node.name = "planner_node"
    graph = MagicMock()
    graph.nodes = {}
    graph.edges = []
    graph.edges_from.return_value = []
    node._graph_ref = graph

    assert node._build_graph_artifacts(_graph_context(None)).knowledge_dir is None


def test_node_artifacts_configure_knowledge_tool_and_hook_state(tmp_path: Path) -> None:
    agent_context = MagicMock()
    from modex_agent.tools.standard.file_tool import EditFileTool, ReadFileTool, WriteFileTool
    tm = InMemoryToolManager()
    tm.register(ReadFileTool())
    tm.register(WriteFileTool())
    tm.register(EditFileTool())
    agent_context.tool_manager = tm
    agent_context.history = MagicMock()
    agent_context.history.to_list = AsyncMock(return_value=[])
    agent_context.history.append = AsyncMock()
    agent_context.runtime = MagicMock()
    agent_context.runtime.state.custom = {}

    builder = MagicMock()
    resolver = _workspace_resolver(tmp_path, builder=builder)
    workspace = resolver.resolve_workspace()

    config = BotAgentNodeConfig.model_validate(
        {
            "agent": "planner",
            "knowledge": {"require_read": True, "require_write": True},
        }
    )
    node = BotAgentNode("planner", "default", resolver, knowledge_config=config.knowledge)
    node.name = "planner_node"
    node.node_id = "node-1"
    graph = MagicMock()
    graph.nodes = {}
    graph.edges_from.return_value = []
    node._graph_ref = graph

    ctx = _graph_context(42)
    artifacts = node._build_graph_artifacts(ctx)
    desc = TurnContextDescriptor(
        agent_kind=AgentCommKind.NORMAL,
        execution_strategy=ExecutionStrategyKind.REACT,
        graph_node_name=node.name,
        graph_instance_id=42,
        is_node_execution=True,
        graph_artifacts=artifacts,
    )
    GraphToolConfigurator().configure(agent_context, desc)
    GraphKnowledgeConfigurator().configure(agent_context, desc)

    knowledge_dir = workspace.ctx.paths.graph_instance_knowledge_dir(42)
    assert agent_context.tool_manager.get_tool("deliver") is not None
    assert isinstance(agent_context.tool_manager.get_tool("knowledge_base"), GraphKnowledgeBaseTool)
    assert agent_context.runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_DIR] == str(
        knowledge_dir
    )
    assert agent_context.runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_READ] is True
    assert agent_context.runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_WRITE] is True
