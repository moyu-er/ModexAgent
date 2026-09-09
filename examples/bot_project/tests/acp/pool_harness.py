"""Hermetic real pool assembly for editor integration tests."""
from __future__ import annotations

from pathlib import Path

from bot.acp.emitter import AcpEmitterHub, AcpOutputAdapter, AcpTurnEmitter
from bot.acp.runtime import AcpEntryConfig, AcpRuntime, _NullInputAdapter
from bot.input_pipeline.context import BotInputContext
from bot.webui.transcript_store import JSONLTranscriptStore

from modex_agent.agents.react.agent import ReActAgent
from modex_agent.approval.ui import IMUserInterface
from modex_agent.core.agent import AgentCommKind
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.provider import LLMProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import Tool
from modex_agent.ioc.configs.approval import ApprovalConfig, ToolApprovalEntry
from modex_agent.ioc.factories.approval import build_approval_runtime
from modex_agent.memory.context import InMemoryContextManager
from modex_agent.messaging.broker import AddressKind
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.messaging.models import BrokerInputPayload, InputMessage
from modex_agent.multi_agent import AgentDescriptor, AgentFactory, AgentPool
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.descriptor import AgentInstance
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox.server_memory import InMemoryInboxServer
from modex_agent.multi_agent.inbox_poller import InboxPoller
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.session_tree.request_scope import REQUEST_SCOPE_ID_KEY
from modex_agent.multi_agent.session_tree.store_node import InMemoryTreeNodeStore
from modex_agent.multi_agent.session_tree.store_track import InMemoryMessageTrackStore
from modex_agent.multi_agent.session_tree.store_tree import InMemorySessionTreeStore
from modex_agent.persistence.session_registry import InMemorySessionRegistry
from modex_agent.pipeline.approval_renderer import ApprovalRenderer
from modex_agent.pipeline.approval_resumer import ApprovalResumer
from modex_agent.pipeline.pipeline import AgentPipeline
from modex_agent.pipeline.turn_context_builder import TurnContextBuilder
from modex_agent.pipeline.turn_runner import ReActTurnRunner
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.runtime.services import AgentRuntimeServices
from modex_agent.runtime.store import InMemoryTurnStateStore
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.utils.sanitizer import ContentSanitizer


class _WriteTool(Tool):
    def __init__(self, writes: list[str]) -> None:
        super().__init__(name="write", description="record writes", parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
        self.writes = writes

    async def execute(self, path: str = "", **kwargs: object) -> str:
        self.writes.append(path)
        return "written"


class _Factory(AgentFactory):
    async def create_agent(self, descriptor, **kwargs):
        raise AssertionError("Resident instance is already assembled")


class _Runtime(AcpRuntime):
    _test_pool: AgentPool
    _broker: InMemoryMessageBroker
    _shared_tools: InMemoryToolManager

    @property
    def pool(self) -> AgentPool:
        return self._test_pool

    async def close(self) -> None:
        await self.pool.stop_poller()
        await self._broker.stop()


def _build_pipeline(
    runtime: _Runtime,
    provider: LLMProvider,
    transcript: JSONLTranscriptStore,
    output,
    *,
    approval: bool,
    root: Path,
):
    turn_store = InMemoryTurnStateStore()
    agent = ReActAgent(provider)
    registry = TurnSessionRegistry()
    safety = RuntimeSafetyPolicy()
    ui = IMUserInterface(output_adapter=output)
    services = AgentRuntimeServices(turn_store=turn_store, approval=build_approval_runtime(
        ApprovalConfig(enabled=True, tools={"write": ToolApprovalEntry(allowed_paths=["./allowed/*"])}),
        project_root=root,
    ) if approval else None)
    builder = TurnContextBuilder(
        agent=agent, tool_manager=runtime._shared_tools, sanitizer=ContentSanitizer.sanitize,
        command_processor=None, skill_resolver=None, context_builder=None, agent_descriptor=None,
        max_iterations=5, safety=safety, runtime_services=services, runtime_context_manager=None,
        governance=None, hook_runner=None, interceptor_chain=None, control_channel=None,
                emitter_factory=lambda sid: AcpTurnEmitter(runtime.hub, sid, pool="main", transcript_store=transcript),
        output_adapter=output, turn_store=turn_store, registry=registry,
    )
    runner = ReActTurnRunner(
        agent=agent, context_manager=InMemoryContextManager(), context_manager_factory=None,
        on_session_start=None, on_session_end=None, safety=safety, turn_store=turn_store,
        registry=registry, builder=builder,
        resumer=ApprovalResumer(agent=agent, turn_store=turn_store, user_interface=ui),
        approval=ApprovalRenderer(agent=agent, user_interface=ui), workspace_manager=None,
        pool_name=None, pool_data_resolver=None, agent_descriptor=None,
    )
    pipeline = AgentPipeline(agent=agent, turn_runner=runner, input_adapter=_NullInputAdapter(), output_adapter=output, registry=registry, safety=safety)
    runner.bind_to_pipeline(pipeline)
    return pipeline


async def build_runtime(root: Path, provider: LLMProvider, *, approval: bool,
                        child_provider: LLMProvider | None = None, child_approval: bool = False) -> tuple[AcpRuntime, list[str]]:
    from bot.input_pipeline.assembly import build_acp_pipeline
    from bot.input_pipeline.stages.skill_parse import PoolSkillResolverRegistry
    from plugins.im_input_stages import IMInputStagesPlugin

    from modex_agent.multi_agent.pool_router import PoolSessionStore
    from modex_agent.plugins.assembly.context import AssemblyContext
    from modex_agent.plugins.registry import ComponentRegistry

    runtime = _Runtime(root, AcpEntryConfig())
    runtime.hub = AcpEmitterHub(resolver=runtime._root_session_for)
    runtime.output = AcpOutputAdapter(runtime.hub)
    runtime._project_root = root
    output = runtime.output
    transcript = JSONLTranscriptStore(root / "sessions")
    writes: list[str] = []
    tools = InMemoryToolManager()
    tools.register(_WriteTool(writes))
    runtime._shared_tools = tools
    pipeline = _build_pipeline(runtime, provider, transcript, output, approval=approval, root=root)
    child_pipeline = (
        _build_pipeline(runtime, child_provider, transcript, output, approval=child_approval, root=root)
        if child_provider is not None
        else None
    )
    sessions = InMemorySessionRegistry()
    broker = InMemoryMessageBroker()
    await broker.start()
    inbox = InMemoryInboxServer()
    consumer = InboxConsumer(server=inbox)
    bus = LocalAgentMessageBus(producer=InboxProducer(server=inbox), consumer=consumer)
    pool = AgentPool(broker=broker, agent_factory=_Factory(), agent_bus=bus, inbox_consumer=consumer, session_registry=sessions)
    descriptor = AgentDescriptor(address=AgentAddress(name="main"), system_prompt_template="test", max_iterations=5)
    await pool.register_resident(descriptor, AgentInstance(descriptor=descriptor, context_manager=InMemoryContextManager(), pipeline=pipeline))
    if child_pipeline is not None:
        sub_descriptor = AgentDescriptor(
            address=AgentAddress(name="sub"), system_prompt_template="test",
            max_iterations=5, comm_kind=AgentCommKind.SUBAGENT,
        )
        child_instance = AgentInstance(descriptor=sub_descriptor, context_manager=InMemoryContextManager(), pipeline=child_pipeline)
        await pool.register_resident(sub_descriptor, child_instance)
    poller = InboxPoller(pool, interval=0.01, session_registry=sessions)
    tree = SessionTreeManager(InMemorySessionTreeStore(), InMemoryTreeNodeStore(), InMemoryMessageTrackStore(), bus, poller, "main", str(root), sessions)
    consumer.set_on_consumed(tree.on_consumed)
    poller.attach_tree_manager(tree)
    pool.attach_poller(poller)
    pool.tree = tree
    runtime._test_pool = pool
    runtime._broker = broker
    routing = PoolSessionStore(root / "routes")
    components = ComponentRegistry()
    from modex_agent.plugins.loader import PluginRegistrationContext
    with PluginRegistrationContext(components) as registration:
        IMInputStagesPlugin().register(registration)
    from modex_agent.workspace.context import WorkspaceContext
    workspace = WorkspaceContext.from_target(root, data_dir_name=".modex", home=root)
    runtime.preparation = await build_acp_pipeline(registry=components, ctx=AssemblyContext(registry=components, workspace_ctx=workspace), skill_registry=PoolSkillResolverRegistry(lambda ws, name: None))
    runtime.input_context = BotInputContext(default_pool="main", pool_session_store=routing, agent_resolver=lambda name: "main", transcript_store=transcript, enqueue_message=lambda msg: (_ for _ in ()).throw(AssertionError("ACP must not enqueue through adapter")), command_adapter=_NullInputAdapter(), current_ws_provider=lambda: root, available_pools=lambda: {"main"})
    pool.start_poller()
    return runtime, writes


async def deliver_child_task(
    runtime: AcpRuntime,
    *,
    root_sid: str,
    scope_id: str,
    child_session: SessionInfo,
    text: str,
) -> None:
    """Deliver one REAL scoped task to a child session inside a live request.

    Mirrors the production SendStrategy shape (SubagentDispatchStrategy): a
    TASK_REQUEST carrier stamped with the running request's scope token,
    delivered through the tree, executed by the real poller as a child turn.
    """
    pool = runtime.pool
    tree = pool.tree
    assert tree is not None, "pool tree must be assembled before scoped child delivery"
    payload = BrokerInputPayload.from_input_message(
        InputMessage(
            content=text,
            session=child_session,
            channel="acp",
            source="acp",
            metadata={"message_id": f"task-{child_session.session_id}"},
        ),
        agent_session_id=child_session.session_id,
        message_type=AgentMessageType.TASK_REQUEST.value,
    )
    envelope = AgentMessageEnvelope(
        payload=payload.model_dump(mode="json", exclude_none=True),
        source=AgentAddress(kind=AddressKind.AGENT, name=SessionInfo.from_str(root_sid).agent_name),
        target=AgentAddress(kind=AddressKind.AGENT, name=child_session.agent_name),
        message_type=AgentMessageType.TASK_REQUEST,
        session_id=child_session.session_id_prefix,
        agent_session_id=child_session.session_id,
        parent_session_id=root_sid,
        metadata={REQUEST_SCOPE_ID_KEY: scope_id},
    )
    await tree.deliver(child_session.session_id, envelope, track_consume=True)
