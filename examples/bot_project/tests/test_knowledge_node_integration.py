from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from bot.graph.agent_node import BotAgentNode
from bot.graph.agent_node_factory import BotAgentNodeConfig, BotAgentNodeFactory

from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_registry import InMemorySessionRegistry
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.pipeline.turn_runner import ReActTurnRunner
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.tools.graph_knowledge_tool import GraphKnowledgeBaseTool
from modex_agent.workspace.paths import WorkspacePaths
from modex_graph.context import GraphContext
from modex_graph.integration import GraphPayload, IntegratedInput
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


def test_ensure_knowledge_tool_creates_instance_scoped_tool(tmp_path: Path) -> None:
    resolver = _workspace_resolver(tmp_path)
    workspace = resolver.resolve_workspace()
    node = BotAgentNode("planner", "default", resolver)
    node.name = "planner_node"

    knowledge_dir = workspace.ctx.paths.graph_instance_knowledge_dir(42)
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.tools.standard.file_tool import EditFileTool, ReadFileTool, WriteFileTool
    tm = InMemoryToolManager()
    tm.register(ReadFileTool())
    tm.register(WriteFileTool())
    tm.register(EditFileTool())
    tool = node._ensure_knowledge_tool(knowledge_dir, tm)

    assert isinstance(tool, GraphKnowledgeBaseTool)
    assert knowledge_dir.is_dir()
    action_schema = tool.get_dynamic_schema()["function"]["parameters"]["properties"]["action"]
    assert action_schema["enum"] == ["read", "ls", "grep", "write", "edit"]


def test_ensure_knowledge_tool_derives_capabilities_from_tools(tmp_path: Path) -> None:
    resolver = _workspace_resolver(tmp_path)
    workspace = resolver.resolve_workspace()
    node = BotAgentNode("planner", "default", resolver)
    node.name = "planner_node"

    knowledge_dir = workspace.ctx.paths.graph_instance_knowledge_dir(42)
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.tools.standard.file_tool import ReadFileTool
    tm = InMemoryToolManager()
    tm.register(ReadFileTool())
    tool = node._ensure_knowledge_tool(knowledge_dir, tm)

    assert isinstance(tool, GraphKnowledgeBaseTool)
    action_schema = tool.get_dynamic_schema()["function"]["parameters"]["properties"]["action"]
    assert "write" not in action_schema["enum"]
    assert "edit" not in action_schema["enum"]
    assert "read" in action_schema["enum"]


def test_ensure_knowledge_tool_returns_none_when_disabled(tmp_path: Path) -> None:
    resolver = _workspace_resolver(tmp_path)
    config = BotAgentNodeConfig.model_validate(
        {"agent": "planner", "knowledge": {"enabled": False}}
    )
    node = BotAgentNode("planner", "default", resolver, knowledge_config=config.knowledge)

    from modex_agent.core.tool_manager import InMemoryToolManager
    assert node._ensure_knowledge_tool(None, InMemoryToolManager()) is None


def test_ensure_knowledge_tool_returns_none_without_graph_instance(tmp_path: Path) -> None:
    resolver = _workspace_resolver(tmp_path)
    node = BotAgentNode("planner", "default", resolver)

    from modex_agent.core.tool_manager import InMemoryToolManager
    assert node._ensure_knowledge_tool(None, InMemoryToolManager()) is None


async def test_execute_injects_knowledge_tool_and_hook_state(tmp_path: Path) -> None:
    result = AgentResult(content="", messages=[], stop_reason=StopReason.COMPLETED)
    turn_runner = MagicMock(spec=ReActTurnRunner)
    turn_runner.execute_turn = AsyncMock(return_value=result)

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
    builder.assemble = AsyncMock(return_value=MagicMock())
    builder.build_runtime_and_context.return_value = (agent_context, MagicMock())
    resolver = _workspace_resolver(tmp_path, builder=builder)
    workspace = resolver.resolve_workspace()
    workspace.pools["default"].pool.get.return_value.pipeline._turn_runner = turn_runner

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
    await node.execute(ctx, IntegratedInput(payloads=[]))

    knowledge_dir = workspace.ctx.paths.graph_instance_knowledge_dir(42)
    assert agent_context.tool_manager.get_tool("deliver") is not None
    assert isinstance(agent_context.tool_manager.get_tool("knowledge_base"), GraphKnowledgeBaseTool)
    assert agent_context.runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_DIR] == str(
        knowledge_dir
    )
    assert agent_context.runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_READ] is True
    assert agent_context.runtime.state.custom[TurnCustomKey.GRAPH_KNOWLEDGE_REQUIRE_WRITE] is True
