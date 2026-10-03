from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modex_agent.agents.external.agent import ExternalAgent
    from modex_agent.commands.skill import SkillResolver
    from modex_agent.control.channel import InMemoryControlChannel
    from modex_agent.core.provider import LLMProvider

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from modex_agent.multi_agent.session_tree.session_binding import SessionBindingStore
    from modex_agent.plugins.defaults.capabilities.approval.renderer import (
        ApprovalRenderer,
    )
    from modex_agent.plugins.defaults.capabilities.approval.resumer import (
        ApprovalResumer,
    )

from modex_agent.core import AgentCommKind
from modex_agent.core.inbox import InboxServer
from modex_agent.core.tool_manager import ToolManager
from modex_agent.hook import HookRunner
from modex_agent.memory.context import ContextManager, InMemoryContextManager
from modex_agent.multi_agent.inbox.flush_hook import InboxFlushHook
from modex_agent.persistence.session_registry import SessionRegistry
from modex_agent.providers.factory import create_llm_provider
from modex_agent.providers.llm_config import LLMConfig
from modex_agent.runtime.context import RuntimeContextManager
from modex_agent.tools.filter import FilteredToolManager
from modex_agent.tools.manager import InMemoryToolManager

from .descriptor import AgentDescriptor, AgentInstance
from .inbox.consumer import InboxConsumer
from .inbox.producer import InboxProducer

if TYPE_CHECKING:
    pass

if TYPE_CHECKING:
    from modex_agent.core.agent import Agent
    from modex_agent.pipeline.turn_runner_abc import TurnRunner


class AgentFactory(ABC):
    """Agent 工厂抽象基类。"""

    @abstractmethod
    async def create_agent(
        self,
        descriptor: AgentDescriptor,
        session_id: str | None = None,
        context_manager: ContextManager | None = None,
        broker: Any | None = None,
        tool_manager: ToolManager | None = None,
        skill_resolver: SkillResolver | None = None,
        sanitizer: Any | None = None,
        command_interceptor: Any | None = None,
        subagent_service: Any | None = None,
        hooks: list[Any] | None = None,
        output_adapter: Any | None = None,
        context_manager_factory: Callable[[str], ContextManager] | None = None,
        llm_provider: LLMProvider | None = None,
    ) -> AgentInstance:
        """根据描述符创建 AgentInstance。

        ``llm_provider`` is the per-agent LLM provider resolved by the
        caller's assembly (the LLM_PROVIDER slot product). When ``None`` the
        factory falls back to its own default, then to
        ``create_llm_provider`` construction.
        """
        ...


class DefaultAgentFactory(AgentFactory):
    """默认 Agent 工厂实现。"""

    def __init__(
        self,
        default_llm_provider: Any | None = None,
        default_tool_manager: ToolManager | None = None,
        sanitizer: Any | None = None,
        command_interceptor: Any | None = None,
        subagent_service: Any | None = None,
        inbox_server: InboxServer | None = None,
        inbox_consumer: InboxConsumer | None = None,
        default_hooks: list[Any] | None = None,
        default_hook_runner: Any | None = None,
        default_interceptor_chain: Any | None = None,
        default_turn_store: Any | None = None,
        control_channel: InMemoryControlChannel | None = None,
        session_registry: SessionRegistry | None = None,
        approval_declared: bool = False,
    ) -> None:
        self._default_llm_provider = default_llm_provider
        self._default_tool_manager = default_tool_manager
        self._sanitizer = sanitizer
        self._command_interceptor = command_interceptor
        self._subagent_service = subagent_service
        self._inbox_server = inbox_server
        self._default_hooks = list(default_hooks) if default_hooks else []
        self._default_hook_runner = default_hook_runner
        self._default_interceptor_chain = default_interceptor_chain
        self._default_turn_store = default_turn_store
        self._control_channel = control_channel
        self._session_registry = session_registry
        self._approval_declared = approval_declared
        self._inbox_producer = InboxProducer(inbox_server) if inbox_server else None
        self._inbox_consumer = inbox_consumer
        # Shared runtime-context manager across all agents created by this factory.
        # Per-session isolation is keyed directly by session_id.
        self._runtime_context_manager = RuntimeContextManager()

    def _resolve_llm_provider(
        self,
        descriptor: AgentDescriptor,
        llm_provider: LLMProvider | None = None,
    ) -> Any:
        """Resolve the per-agent LLM provider.

        Priority: per-agent override → factory default → last-resort
        ``create_llm_provider`` construction (OPENAI_COMPATIBLE, per
        ``LLMConfig`` default). The fallback semantics are unchanged from
        the legacy SDK provider path: the final resort when no provider was
        injected anywhere; the empty ``api_key`` defers to the
        ``OPENAI_API_KEY`` environment variable (T18 env fallback), matching
        the old SDK-path behaviour.
        """
        if llm_provider is not None:
            return llm_provider
        if self._default_llm_provider is not None:
            return self._default_llm_provider
        cfg = descriptor.llm_config
        # AgentLLMConfig.max_output_tokens is optional; LLMConfig requires an int.
        return create_llm_provider(
            LLMConfig(
                model=cfg.model or "gpt-4o",
                temperature=cfg.temperature,
                max_output_tokens=(
                    cfg.max_output_tokens if cfg.max_output_tokens is not None else 80000
                ),
                reasoning_effort=cfg.reasoning_effort,
            )
        )

    def _build_agent(self, descriptor: AgentDescriptor, provider: Any) -> Agent:
        """Build the ReAct agent (the factory's one runtime shape, W5).

        The former ``_get_builder`` enum dispatch (REACT/PIPELINE/EXTERNAL)
        died with the runtime-slot wave: the RUNTIME is an
        EXECUTION_STRATEGY slot product — self-owning shapes (``external``)
        build their runtime through their own strategy assembly, and
        native-component custom loops plug their own constructor into
        ``assemble_native_agent``. This factory is the bundled react
        runtime constructor.
        """
        from modex_agent.agents.react.builder import ReActAgentBuilder

        return ReActAgentBuilder.build_agent(descriptor, provider)

    def _resolve_context_manager(
        self,
        descriptor: AgentDescriptor,
        session_id: str | None = None,
        provided_context_manager: ContextManager | None = None,
    ) -> ContextManager:
        """根据 descriptor 的 context_strategy 解析合适的 ContextManager。"""
        if provided_context_manager is not None:
            return provided_context_manager
        if descriptor.context_manager is not None:
            return descriptor.context_manager
        if descriptor.context_strategy == "ephemeral":
            # 每次创建全新的 InMemoryContextManager，不保留历史、不写入文件
            return InMemoryContextManager(
                base_system_prompt=descriptor.system_prompt_template or ""
            )
        # persistent / shared 默认使用标准内存管理器
        return InMemoryContextManager(base_system_prompt=descriptor.system_prompt_template or "")

    def _build_turn_runner(
        self,
        *,
        agent: Agent,
        descriptor: AgentDescriptor,
        ctx_mgr: ContextManager,
        filtered_tools: Any,
        skill_resolver: SkillResolver | None,
        hook_runner: HookRunner,
        agent_interceptor_chain: Any,
        context_manager_factory: Callable[[str], ContextManager] | None,
        emitter_factory: Callable[..., Any] | None,
        pipe_output_adapter: Any,
        registry: Any,
        turn_store: Any,
        runtime_context_manager: RuntimeContextManager,
        safety: Any,
        subagent_governance: Any,
    ) -> TurnRunner:
        from modex_agent.pipeline.turn_context_builder import TurnContextBuilder
        from modex_agent.pipeline.turn_runner import ReActTurnRunner
        from modex_agent.runtime.services import AgentRuntimeServices
        from modex_agent.utils.sanitizer import ContentSanitizer

        sanitizer = ContentSanitizer.sanitize
        descriptor_model_info = (
            descriptor.llm_config.model_info if descriptor.llm_config is not None else None
        )
        runtime_services = (
            AgentRuntimeServices(model_info=descriptor_model_info)
            if descriptor_model_info is not None
            else None
        )
        turn_context_builder = TurnContextBuilder(
            agent=agent,
            tool_manager=filtered_tools,
            sanitizer=sanitizer,
            command_processor=None,
            skill_resolver=skill_resolver,
            context_builder=None,
            agent_descriptor=descriptor,
            max_iterations=descriptor.max_iterations,
            safety=safety,
            runtime_services=runtime_services,
            runtime_context_manager=runtime_context_manager,
            governance=subagent_governance,
            hook_runner=hook_runner,
            interceptor_chain=agent_interceptor_chain,
            control_channel=self._control_channel,
            emitter_factory=emitter_factory,
            output_adapter=pipe_output_adapter,
            turn_store=turn_store,
            registry=registry,
        )
        # The approval collaborators exist only when the pool's root agent
        # declares approval (a root-only declaration). Without it no gate is
        # ever assembled — nothing can suspend, so the resumer/renderer
        # paths are unreachable and None is the undeclared-boot shape; the
        # approval bundle's implementation stays unloaded. Guard-only
        # sandbox deployments never suspend either, so they need no
        # resumer.
        approval_state_machine: ApprovalResumer | None = None
        approval: ApprovalRenderer | None = None
        if self._approval_declared:
            from modex_agent.plugins.defaults.capabilities.approval.renderer import (
                ApprovalRenderer,
            )
            from modex_agent.plugins.defaults.capabilities.approval.resumer import (
                ApprovalResumer,
            )

            approval_state_machine = ApprovalResumer(
                agent=agent,
                turn_store=turn_store,
                user_interface=None,
            )
            approval = ApprovalRenderer(
                agent=agent,  # type: ignore[arg-type]
                user_interface=None,
            )
        return ReActTurnRunner(
            agent=agent,
            context_manager=ctx_mgr,
            context_manager_factory=context_manager_factory,
            on_session_start=None,
            on_session_end=None,
            safety=safety,
            turn_store=turn_store,
            registry=registry,
            builder=turn_context_builder,
            resumer=approval_state_machine,
            approval=approval,
            workspace_manager=None,
            pool_name=None,
            pool_data_resolver=None,
            agent_descriptor=descriptor,
        )

    async def create_agent(
        self,
        descriptor: AgentDescriptor,
        session_id: str | None = None,
        context_manager: ContextManager | None = None,
        broker: Any | None = None,
        tool_manager: ToolManager | None = None,
        skill_resolver: SkillResolver | None = None,
        sanitizer: Any | None = None,
        command_interceptor: Any | None = None,
        subagent_service: Any | None = None,
        hooks: list[Any] | None = None,
        output_adapter: Any | None = None,
        context_manager_factory: Callable[[str], ContextManager] | None = None,
        llm_provider: LLMProvider | None = None,
    ) -> AgentInstance:
        provider = self._resolve_llm_provider(descriptor, llm_provider)
        agent = self._build_agent(descriptor, provider)

        ctx_mgr = self._resolve_context_manager(descriptor, session_id, context_manager)

        tool_mgr = tool_manager or self._default_tool_manager or InMemoryToolManager()
        filtered_tools = FilteredToolManager(
            base=tool_mgr,
            allowed_tools=descriptor.allowed_tools,
            denied_tools=descriptor.denied_tools,
        )

        # Native assembly passes the resolver bound for this exact agent.
        # ``None`` is an explicit absence (for example, a capability veto),
        # never a request to inherit another agent's resolver.
        resolver = skill_resolver

        auto_inbox_flush = (
            InboxFlushHook(
                consumer=self._inbox_consumer,
                agent_name=descriptor.address.name,
            )
            if descriptor.inbox_strategy != "none" and self._inbox_consumer is not None
            else None
        )

        subagent_governance: Any | None = None
        if descriptor.comm_kind == AgentCommKind.SUBAGENT:
            from modex_agent.memory.assembly import create_subagent_governance
            from modex_agent.memory.system import MemorySystemContextManager

            # A subagent's turn runner loads through THIS context manager
            # (template materialization builds one MemorySystemContextManager
            # per subagent; turn_runner.py never overrides a subagent's CM
            # with the pool's), so the memory-compaction governance binds to
            # it: same MemoryContext instances load() builds — the T3 hard
            # constraint. Non-memory CMs keep the repair-only chain
            # (narrowing precedent: native_core.py:551).
            memory_cm = ctx_mgr if isinstance(ctx_mgr, MemorySystemContextManager) else None
            memory_system = memory_cm.memory_system if memory_cm is not None else None
            subagent_governance = create_subagent_governance(
                descriptor.memory_config,
                memory_system=memory_system,
                memory_context_resolver=(
                    (lambda actx: memory_cm.resolve_memory_context(actx.session.session_id))
                    if memory_system is not None
                    else None
                ),
            )

        from modex_agent.messaging.broker_memory import InMemoryMessageBroker
        from modex_agent.multi_agent.router import DefaultMeshRouter
        from modex_agent.pipeline.broker_bridge import (
            BrokerInputAdapter,
            BrokerOutputAdapter,
        )
        from modex_agent.pipeline.pipeline import AgentPipeline
        from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry

        if broker is None:
            logger.warning(
                "Creating pipeline agent with isolated broker. Pass broker= for mesh communication."
            )
            broker = InMemoryMessageBroker()
            await broker.start()
        address = descriptor.address
        input_adapter = BrokerInputAdapter(broker=broker, address=address)
        from modex_agent.adapters.output import OutputAdapter

        if output_adapter is not None and isinstance(output_adapter, OutputAdapter):
            pipe_output_adapter = output_adapter
            emitter_output_adapter = output_adapter
        else:
            pipe_output_adapter = BrokerOutputAdapter(
                broker=broker,
                sender=address,
                default_topic=f"agent:{address.name}:out",
            )
            emitter_output_adapter = BrokerOutputAdapter(
                broker=broker, sender=address, default_topic=f"agent:{address.name}:out"
            )
        from modex_agent.agents.react.builder import ReActAgentBuilder

        emitter_factory = ReActAgentBuilder.build_emitter_factory(emitter_output_adapter)
        default_hook_specs = (
            self._default_hook_runner.hook_specs
            if self._default_hook_runner is not None
            else []
        )
        hook_runner = HookRunner()
        agent_interceptor_chain = None
        if self._default_interceptor_chain is not None:
            from modex_agent.interceptor.chain import InterceptorChain

            agent_interceptor_chain = InterceptorChain(self._default_interceptor_chain.interceptors)

        registry = TurnSessionRegistry()
        turn_runner = self._build_turn_runner(
            agent=agent,
            descriptor=descriptor,
            ctx_mgr=ctx_mgr,
            filtered_tools=filtered_tools,
            skill_resolver=resolver,
            hook_runner=hook_runner,
            agent_interceptor_chain=agent_interceptor_chain,
            context_manager_factory=context_manager_factory,
            emitter_factory=emitter_factory,
            pipe_output_adapter=pipe_output_adapter,
            registry=registry,
            turn_store=self._default_turn_store,
            runtime_context_manager=self._runtime_context_manager,
            safety=descriptor.safety_policy,
            subagent_governance=subagent_governance,
        )
        pipeline = AgentPipeline(
            agent=agent,
            turn_runner=turn_runner,
            input_adapter=input_adapter,
            output_adapter=pipe_output_adapter,
            registry=registry,
            safety=descriptor.safety_policy,
            router=DefaultMeshRouter(session_registry=self._session_registry),
            control_channel=self._control_channel,
        )
        turn_runner.bind_to_pipeline(pipeline)

        from modex_agent.hook import HookErrorPolicy, HookSpec

        hook_runner.extend(default_hook_specs)

        # LoopDetectionHook is NOT constructed here: it reaches the runner as a
        # compiler position-default roster row (`loop_detection` in
        # POSITION_DEFAULT_HOOKS), resolved through the HOOK-slot factory like
        # every roster entry — vetoable via `hooks: [-loop_detection]` and
        # visible on the bill with a position_default origin (ADR-0047 W6).
        #
        # ANTI-REGRESSION (ADR-0047 W6): trace span hooks are NOT constructed
        # here either. The retired block (the trace-family builder call + the
        # inline L2 score-injector construction + the add-loop over the
        # observability ctor state) died with the `tracing` capability
        # convergence: the span hooks reach the runner as capability-contributed
        # roster entries (`trace_root`/`trace_chat`/`trace_tool`/
        # `trace_handoff`/`trace_approval`/`trace_agent_start`/
        # `trace_iteration`, resolved through the HOOK-slot factories whose
        # construction authority is TracingCapability.assemble — vetoable via
        # `hooks: [-trace_*]`, tier-gated by the `trace_spans` binding vouch.
        # The same death took the checkpoint/training hooks: both moved to the
        # deployment's shared_hooks (bot wiring), keeping per-turn state
        # semantics (both read ctx.runtime.services per turn). Do NOT
        # reintroduce any observability-driven hook construction here.
        live_hooks: list[Any] = []
        if auto_inbox_flush is not None:
            live_hooks.append(auto_inbox_flush)
        for hook in live_hooks:
            hook_runner.add(HookSpec(hook=hook, on_error=HookErrorPolicy.LOG))

        return AgentInstance(
            descriptor=descriptor,
            context_manager=ctx_mgr,
            pipeline=pipeline,
        )


def assemble_external_pipeline(
    descriptor: AgentDescriptor,
    agent: ExternalAgent,
    broker: Any,
    safety: Any,
    *,
    hook_runner: Any | None = None,
    session_registry: Any | None = None,
    control_channel: Any | None = None,
    output_adapter: Any | None = None,
    context_manager: Any | None = None,
    session_binding_store: SessionBindingStore | None = None,
) -> Any:
    """Assemble broker I/O + emitter + turn runner + pipeline + instance.

    Moved from ``ExternalAgentBuilder.assemble_pipeline`` (W3b): the
    composition builds pipeline + multi-agent objects, so its home is the
    factory that owns the native parallel path — not the agents package the
    layering places below the pipeline.

    Shared by the main-agent path
    (``ExternalExecutionStrategy.assemble_main``) and the subagent path
    (``ExternalExecutionStrategy.assemble_sub``) so the ~40 lines of
    broker-adapter / emitter-factory / turn-runner /
    pipeline / AgentInstance construction live in one place.

    Callers retain their distinct responsibilities: backend/provider
    selection, env-spec construction, and (for subagent) hook
    registration. Only the pipeline-IO-instance assembly is shared.
    """
    from modex_agent.agents.external.builder import ExternalAgentBuilder
    from modex_agent.core.llm_struct import RuntimeSafetyPolicy
    from modex_agent.memory.context import InMemoryContextManager
    from modex_agent.pipeline.broker_bridge import (
        BrokerInputAdapter,
        BrokerOutputAdapter,
    )
    from modex_agent.pipeline.external_turn_runner import ExternalTurnRunner
    from modex_agent.pipeline.pipeline import AgentPipeline
    from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry

    from .descriptor import AgentInstance
    from .router import DefaultMeshRouter

    address = descriptor.address
    input_adapter = BrokerInputAdapter(broker=broker, address=address)

    if output_adapter is not None:
        pipe_output_adapter = output_adapter
        emitter_output_adapter = output_adapter
    else:
        pipe_output_adapter = BrokerOutputAdapter(
            broker=broker,
            sender=address,
            default_topic=f"agent:{address.name}:out",
        )
        emitter_output_adapter = BrokerOutputAdapter(
            broker=broker,
            sender=address,
            default_topic=f"agent:{address.name}:out",
        )

    emitter_factory = ExternalAgentBuilder.build_emitter_factory(emitter_output_adapter)

    registry = TurnSessionRegistry()
    turn_runner = ExternalTurnRunner(
        agent=agent,
        emitter_factory=emitter_factory,
        output_adapter=pipe_output_adapter,
        registry=registry,
        on_session_start=None,
        on_session_end=None,
        safety=safety or RuntimeSafetyPolicy(),
        hook_runner=hook_runner,
        session_binding_store=session_binding_store,
    )

    pipeline = AgentPipeline(
        agent=agent,
        turn_runner=turn_runner,
        input_adapter=input_adapter,
        output_adapter=pipe_output_adapter,
        registry=registry,
        safety=safety or RuntimeSafetyPolicy(),
        router=DefaultMeshRouter(session_registry=session_registry),
        control_channel=control_channel,
    )

    ctx_mgr = context_manager or InMemoryContextManager(base_system_prompt="")
    return AgentInstance(
        descriptor=descriptor,
        context_manager=ctx_mgr,
        pipeline=pipeline,
    )
